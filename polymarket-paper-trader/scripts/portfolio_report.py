#!/usr/bin/env python3
"""
Portfolio Performance Report

Generates detailed analytics for a paper trading portfolio:
- Total and annualized return
- Win rate, Sharpe ratio, Sortino ratio
- Max drawdown, average trade duration
- Best/worst trades
- Output as formatted text or JSON
"""

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import os
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.append(_THIS_DIR)
from paper_engine import (
    DB_PATH,
    _get_db,
    _active_portfolio,
    get_portfolio,
)


def generate_report(portfolio_name: str = "default") -> dict:
    """Generate a full performance report for the portfolio."""
    conn = _get_db()
    try:
        pf = _active_portfolio(conn, portfolio_name)
        pid = pf["id"]
        starting = pf["starting_balance"]

        # Get current state with live prices
        current = get_portfolio(portfolio_name, refresh_prices=True)

        # ----- Trade analysis -----
        trades = conn.execute(
            """SELECT * FROM trades WHERE portfolio_id = ?
               ORDER BY executed_at ASC""",
            (pid,),
        ).fetchall()
        trades = [dict(t) for t in trades]

        # Match buys to sells to compute per-trade P&L
        closed_trades = _match_trades(trades)
        open_positions = current["positions"]

        # ----- Daily snapshots -----
        snapshots = conn.execute(
            """SELECT * FROM daily_snapshots WHERE portfolio_id = ?
               ORDER BY date ASC""",
            (pid,),
        ).fetchall()
        snapshots = [dict(s) for s in snapshots]

        # ----- Core metrics -----
        total_value = current["total_value"]
        total_return = (total_value - starting) / starting if starting else 0

        # Time-based calculations
        created = datetime.fromisoformat(pf["created_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days_active = max((now - created).days, 1)
        years_active = days_active / 365.25

        annualized_return = (
            ((1 + total_return) ** (1 / years_active) - 1)
            if years_active > 0 and total_return > -1 else 0
        )

        # Win rate
        winning = [t for t in closed_trades if t["pnl"] > 0]
        losing = [t for t in closed_trades if t["pnl"] <= 0]
        win_rate = len(winning) / len(closed_trades) if closed_trades else 0

        # Average P&L
        avg_win = (
            sum(t["pnl"] for t in winning) / len(winning) if winning else 0
        )
        avg_loss = (
            sum(t["pnl"] for t in losing) / len(losing) if losing else 0
        )

        # Profit factor
        gross_profit = sum(t["pnl"] for t in winning)
        gross_loss = abs(sum(t["pnl"] for t in losing))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # ----- Drawdown from snapshots -----
        equity_curve = [starting]
        if snapshots:
            equity_curve = [s["total_value"] for s in snapshots]
        max_drawdown, max_dd_duration = _compute_drawdown(equity_curve)

        # ----- Sharpe & Sortino from daily returns -----
        daily_returns = _daily_returns(snapshots, starting)
        sharpe = _sharpe_ratio(daily_returns)
        sortino = _sortino_ratio(daily_returns)

        # ----- Average trade duration -----
        durations = []
        for ct in closed_trades:
            if ct.get("open_time") and ct.get("close_time"):
                try:
                    t_open = datetime.fromisoformat(
                        ct["open_time"].replace("Z", "+00:00")
                    )
                    t_close = datetime.fromisoformat(
                        ct["close_time"].replace("Z", "+00:00")
                    )
                    durations.append((t_close - t_open).total_seconds() / 3600)
                except (ValueError, TypeError):
                    pass
        avg_duration_hours = (
            sum(durations) / len(durations) if durations else 0
        )

        # ----- Best / Worst trades -----
        sorted_by_pnl = sorted(closed_trades, key=lambda t: t["pnl"], reverse=True)
        best_trades = sorted_by_pnl[:3] if sorted_by_pnl else []
        worst_trades = sorted_by_pnl[-3:][::-1] if sorted_by_pnl else []

        # ----- Fees -----
        total_fees = sum(t.get("fee", 0) for t in trades)

        report = {
            "portfolio_name": portfolio_name,
            "generated_at": now.isoformat(),
            "days_active": days_active,
            "summary": {
                "starting_balance": starting,
                "current_value": total_value,
                "cash_balance": current["cash_balance"],
                "positions_value": current["positions_value"],
                "total_return_usd": round(total_value - starting, 2),
                "total_return_pct": round(total_return * 100, 2),
                "annualized_return_pct": round(annualized_return * 100, 2),
            },
            "risk_metrics": {
                "sharpe_ratio": round(sharpe, 3),
                "sortino_ratio": round(sortino, 3),
                "max_drawdown_pct": round(max_drawdown * 100, 2),
                "max_drawdown_duration_days": max_dd_duration,
                "current_drawdown_pct": current["drawdown_pct"],
            },
            "trade_metrics": {
                "total_trades": len(trades),
                "closed_trades": len(closed_trades),
                "open_positions": len(open_positions),
                "win_rate_pct": round(win_rate * 100, 1),
                "avg_win_usd": round(avg_win, 2),
                "avg_loss_usd": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2),
                "total_fees_usd": round(total_fees, 2),
                "avg_trade_duration_hours": round(avg_duration_hours, 1),
            },
            "best_trades": [
                _trade_summary(t) for t in best_trades
            ],
            "worst_trades": [
                _trade_summary(t) for t in worst_trades
            ],
            "open_positions": [
                {
                    "market": p["market_question"],
                    "side": p["side"],
                    "shares": p["shares"],
                    "entry": p["avg_entry"],
                    "current": p["current_price"],
                    "unrealized_pnl": p["unrealized_pnl"],
                }
                for p in open_positions
            ],
        }

        return report

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _match_trades(trades: list[dict]) -> list[dict]:
    """
    Match BUY and SELL trades on the same token/side to compute
    per-round-trip P&L.
    """
    # Group buys by (token_id, side)
    open_lots: dict[tuple, list] = {}
    closed: list[dict] = []

    for t in trades:
        key = (t["token_id"], t["side"])

        if t["action"] == "BUY":
            if key not in open_lots:
                open_lots[key] = []
            open_lots[key].append({
                "shares": t["shares"],
                "price": t["price"],
                "fee": t["fee"],
                "time": t["executed_at"],
                "market": t.get("market_question", ""),
                "reasoning": t.get("reasoning", ""),
            })

        elif t["action"] == "SELL":
            lots = open_lots.get(key, [])
            remaining = t["shares"]
            sell_price = t["price"]
            sell_fee = t["fee"]
            sell_time = t["executed_at"]

            while remaining > 0.0001 and lots:
                lot = lots[0]
                matched = min(remaining, lot["shares"])

                pnl = (sell_price - lot["price"]) * matched - (
                    lot["fee"] * (matched / lot["shares"]) if lot["shares"] > 0 else 0
                ) - (
                    sell_fee * (matched / t["shares"]) if t["shares"] > 0 else 0
                )

                closed.append({
                    "token_id": t["token_id"],
                    "side": t["side"],
                    "market": lot["market"],
                    "shares": round(matched, 4),
                    "entry_price": lot["price"],
                    "exit_price": sell_price,
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(
                        (sell_price - lot["price"]) / lot["price"] * 100, 2
                    ) if lot["price"] > 0 else 0,
                    "open_time": lot["time"],
                    "close_time": sell_time,
                    "reasoning": lot["reasoning"],
                })

                lot["shares"] -= matched
                remaining -= matched
                if lot["shares"] < 0.0001:
                    lots.pop(0)

    return closed


def _compute_drawdown(equity_curve: list[float]) -> tuple[float, int]:
    """Compute max drawdown and its duration in days."""
    if not equity_curve or len(equity_curve) < 2:
        return 0.0, 0

    peak = equity_curve[0]
    max_dd = 0.0
    dd_start = 0
    max_dd_duration = 0
    current_dd_start = 0

    for i, value in enumerate(equity_curve):
        if value >= peak:
            peak = value
            duration = i - current_dd_start
            max_dd_duration = max(max_dd_duration, duration)
            current_dd_start = i
        else:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd
                dd_start = current_dd_start

    # Check if still in drawdown
    if equity_curve[-1] < peak:
        duration = len(equity_curve) - 1 - current_dd_start
        max_dd_duration = max(max_dd_duration, duration)

    return max_dd, max_dd_duration


def _daily_returns(
    snapshots: list[dict],
    starting_balance: float,
) -> list[float]:
    """Extract daily return series from snapshots."""
    if not snapshots:
        return []

    values = [starting_balance] + [s["total_value"] for s in snapshots]
    returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            returns.append((values[i] - values[i - 1]) / values[i - 1])
    return returns


def _sharpe_ratio(
    daily_returns: list[float],
    risk_free_daily: float = 0.0001,  # ~3.7% annual
) -> float:
    """Annualized Sharpe ratio from daily returns."""
    if len(daily_returns) < 2:
        return 0.0

    excess = [r - risk_free_daily for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    variance = sum((r - mean_excess) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(variance) if variance > 0 else 0

    if std == 0:
        return 0.0
    return (mean_excess / std) * math.sqrt(252)


def _sortino_ratio(
    daily_returns: list[float],
    risk_free_daily: float = 0.0001,
) -> float:
    """Annualized Sortino ratio (uses downside deviation only)."""
    if len(daily_returns) < 2:
        return 0.0

    excess = [r - risk_free_daily for r in daily_returns]
    mean_excess = sum(excess) / len(excess)

    downside = [min(0, r) ** 2 for r in excess]
    downside_dev = math.sqrt(sum(downside) / len(downside)) if downside else 0

    if downside_dev == 0:
        return 0.0
    return (mean_excess / downside_dev) * math.sqrt(252)


def _trade_summary(trade: dict) -> dict:
    """Compact summary of a closed trade for reporting."""
    return {
        "market": trade.get("market", "")[:70],
        "side": trade["side"],
        "shares": trade["shares"],
        "entry": trade["entry_price"],
        "exit": trade["exit_price"],
        "pnl_usd": trade["pnl"],
        "pnl_pct": trade["pnl_pct"],
        "duration": trade.get("close_time", ""),
    }


# ---------------------------------------------------------------------------
# Text formatting
# ---------------------------------------------------------------------------

def format_report(report: dict) -> str:
    """Format the report as human-readable text."""
    s = report["summary"]
    r = report["risk_metrics"]
    t = report["trade_metrics"]

    lines = [
        "=" * 60,
        f"  PORTFOLIO REPORT: {report['portfolio_name']}",
        f"  Generated: {report['generated_at'][:19]}",
        f"  Active for: {report['days_active']} days",
        "=" * 60,
        "",
        "--- Performance Summary ---",
        f"  Starting Balance:     ${s['starting_balance']:>12,.2f}",
        f"  Current Value:        ${s['current_value']:>12,.2f}",
        f"  Total Return:         ${s['total_return_usd']:>12,.2f} "
        f"({s['total_return_pct']:+.2f}%)",
        f"  Annualized Return:    {s['annualized_return_pct']:>12.2f}%",
        "",
        "--- Risk Metrics ---",
        f"  Sharpe Ratio:         {r['sharpe_ratio']:>12.3f}",
        f"  Sortino Ratio:        {r['sortino_ratio']:>12.3f}",
        f"  Max Drawdown:         {r['max_drawdown_pct']:>12.2f}%",
        f"  Max DD Duration:      {r['max_drawdown_duration_days']:>12d} days",
        f"  Current Drawdown:     {r['current_drawdown_pct']:>12.2f}%",
        "",
        "--- Trade Metrics ---",
        f"  Total Trades:         {t['total_trades']:>12d}",
        f"  Closed Trades:        {t['closed_trades']:>12d}",
        f"  Open Positions:       {t['open_positions']:>12d}",
        f"  Win Rate:             {t['win_rate_pct']:>12.1f}%",
        f"  Avg Win:              ${t['avg_win_usd']:>12,.2f}",
        f"  Avg Loss:             ${t['avg_loss_usd']:>12,.2f}",
        f"  Profit Factor:        {t['profit_factor']:>12.2f}",
        f"  Total Fees:           ${t['total_fees_usd']:>12,.2f}",
        f"  Avg Trade Duration:   {t['avg_trade_duration_hours']:>12.1f} hours",
    ]

    if report["best_trades"]:
        lines += ["", "--- Best Trades ---"]
        for i, bt in enumerate(report["best_trades"], 1):
            lines.append(
                f"  {i}. {bt['side']} {bt['shares']:.1f}sh "
                f"${bt['entry']:.4f}->${bt['exit']:.4f} "
                f"P&L: ${bt['pnl_usd']:+,.2f} ({bt['pnl_pct']:+.1f}%)"
            )
            if bt.get("market"):
                lines.append(f"     {bt['market']}")

    if report["worst_trades"]:
        lines += ["", "--- Worst Trades ---"]
        for i, wt in enumerate(report["worst_trades"], 1):
            lines.append(
                f"  {i}. {wt['side']} {wt['shares']:.1f}sh "
                f"${wt['entry']:.4f}->${wt['exit']:.4f} "
                f"P&L: ${wt['pnl_usd']:+,.2f} ({wt['pnl_pct']:+.1f}%)"
            )
            if wt.get("market"):
                lines.append(f"     {wt['market']}")

    if report["open_positions"]:
        lines += ["", "--- Open Positions ---"]
        for p in report["open_positions"]:
            lines.append(
                f"  {p['side']} {p['shares']:.1f}sh "
                f"@ ${p['entry']:.4f} -> ${p['current']:.4f} "
                f"P&L: ${p['unrealized_pnl']:+,.2f}"
            )
            if p.get("market"):
                lines.append(f"     {p['market']}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate portfolio performance report",
    )
    parser.add_argument("--portfolio", default="default", help="Portfolio name")
    parser.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args()

    try:
        report = generate_report(args.portfolio)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(format_report(report))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
