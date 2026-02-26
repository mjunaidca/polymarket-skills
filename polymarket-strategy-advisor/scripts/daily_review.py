#!/usr/bin/env python3
"""Analyze paper trading performance and suggest parameter adjustments.

Reads the paper trader's SQLite database, computes performance metrics,
breaks down results by strategy type, and outputs actionable suggestions.

Usage:
    python daily_review.py --portfolio-db ~/.polymarket-paper/portfolio.db
    python daily_review.py --portfolio-db ~/.polymarket-paper/portfolio.db --days 7
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


DEFAULT_DB_PATH = os.path.expanduser("~/.polymarket-paper/portfolio.db")


def connect_db(db_path):
    """Connect to the paper trader database. Returns None if not found."""
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_closed_trades(conn, since_date):
    """Fetch all closed (SELL) trades since a given date.

    The paper_engine schema stores BUY and SELL as separate trade rows.
    A 'closed' trade is a SELL action. We join with the position to get
    entry price for P&L calculation.
    """
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                t.market_question,
                t.side,
                t.action,
                t.price as exit_price,
                t.shares,
                t.total_cost,
                t.fee,
                t.reasoning,
                t.executed_at as closed_at,
                -- Calculate realized P&L: for SELL trades, profit = (sell_price - avg_entry) * shares
                COALESCE(
                    (SELECT b.price FROM trades b
                     WHERE b.token_id = t.token_id AND b.action = 'BUY'
                     AND b.portfolio_id = t.portfolio_id
                     ORDER BY b.executed_at DESC LIMIT 1),
                    t.price
                ) as entry_price
            FROM trades t
            WHERE t.action = 'SELL'
              AND t.executed_at >= ?
            ORDER BY t.executed_at DESC
            """,
            (since_date.isoformat(),),
        )
        rows = [dict(row) for row in cur.fetchall()]
        # Calculate realized P&L for each trade
        for r in rows:
            entry = float(r.get("entry_price", 0))
            exit_p = float(r.get("exit_price", 0))
            shares = float(r.get("shares", 0))
            fee = float(r.get("fee", 0))
            r["realized_pnl"] = round((exit_p - entry) * shares - fee, 4)
        return rows
    except sqlite3.OperationalError:
        return []


def get_open_positions(conn):
    """Fetch currently open positions."""
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                market_question,
                token_id,
                side,
                avg_entry as entry_price,
                shares,
                current_price,
                opened_at
            FROM positions
            WHERE closed = 0
            ORDER BY opened_at DESC
            """
        )
        return [dict(row) for row in cur.fetchall()]
    except sqlite3.OperationalError:
        return []


def get_account_history(conn, since_date):
    """Fetch portfolio value history from daily_snapshots."""
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT total_value as portfolio_value, cash_balance as cash, date as updated_at
            FROM daily_snapshots
            WHERE date >= ?
            ORDER BY date ASC
            """,
            (since_date.strftime("%Y-%m-%d"),),
        )
        return [dict(row) for row in cur.fetchall()]
    except sqlite3.OperationalError:
        return []


def compute_metrics(trades):
    """Compute performance metrics from a list of closed trades."""
    if not trades:
        return {
            "total_trades": 0,
            "winners": 0,
            "losers": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
            "largest_winner": 0.0,
            "largest_loser": 0.0,
            "profit_factor": 0.0,
            "avg_hold_time_hours": 0.0,
        }

    pnls = [float(t.get("realized_pnl", 0)) for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)

    gross_profit = sum(winners) if winners else 0
    gross_loss = abs(sum(losers)) if losers else 0

    # Hold time calculation
    hold_times = []
    for t in trades:
        opened = t.get("opened_at", "")
        closed = t.get("closed_at", "")
        if opened and closed:
            try:
                o = datetime.fromisoformat(opened)
                c = datetime.fromisoformat(closed)
                hold_times.append((c - o).total_seconds() / 3600)
            except (ValueError, TypeError):
                pass

    return {
        "total_trades": len(trades),
        "winners": len(winners),
        "losers": len(losers),
        "breakeven": len(trades) - len(winners) - len(losers),
        "win_rate": len(winners) / len(trades) if trades else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0,
        "avg_winner": round(gross_profit / len(winners), 2) if winners else 0,
        "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
        "largest_winner": round(max(winners), 2) if winners else 0,
        "largest_loser": round(min(losers), 2) if losers else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_hold_time_hours": round(sum(hold_times) / len(hold_times), 1) if hold_times else 0,
    }


def compute_drawdown(account_history):
    """Compute max drawdown from account history."""
    if not account_history:
        return {"max_drawdown_pct": 0, "current_drawdown_pct": 0}

    values = [float(h["portfolio_value"]) for h in account_history]
    peak = values[0]
    max_dd = 0
    for v in values:
        peak = max(peak, v)
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    current_peak = max(values)
    current_dd = (current_peak - values[-1]) / current_peak if current_peak > 0 else 0

    return {
        "max_drawdown_pct": round(max_dd * 100, 2),
        "current_drawdown_pct": round(current_dd * 100, 2),
    }


def breakdown_by_strategy(trades):
    """Break down metrics by edge_type."""
    strategies = {}
    for t in trades:
        edge_type = t.get("edge_type", "unknown") or "unknown"
        if edge_type not in strategies:
            strategies[edge_type] = []
        strategies[edge_type].append(t)

    result = {}
    for strategy, strades in strategies.items():
        result[strategy] = compute_metrics(strades)

    return result


def generate_suggestions(metrics, strategy_breakdown, drawdown, open_positions):
    """Generate actionable parameter adjustment suggestions."""
    suggestions = []

    # Overall performance
    if metrics["total_trades"] == 0:
        suggestions.append(
            "No closed trades in this period. Start by running the advisor "
            "to find opportunities and executing paper trades."
        )
        return suggestions

    # Win rate analysis
    if metrics["win_rate"] < 0.40 and metrics["total_trades"] >= 10:
        suggestions.append(
            f"Win rate is {metrics['win_rate']:.0%} (below 40% threshold). "
            f"Consider tightening entry criteria: increase --min-edge to 0.05 "
            f"or raise minimum confidence to 0.7."
        )
    elif metrics["win_rate"] > 0.70 and metrics["total_trades"] >= 10:
        suggestions.append(
            f"Win rate is {metrics['win_rate']:.0%} (strong). Consider "
            f"slightly increasing position sizes if risk limits allow."
        )

    # Profit factor
    if metrics["profit_factor"] < 1.0 and metrics["total_trades"] >= 5:
        suggestions.append(
            f"Profit factor is {metrics['profit_factor']:.2f} (below 1.0 = "
            f"losing money). Review: are stop losses being honored? Are "
            f"winners being closed too early?"
        )

    # Winner/loser ratio
    if metrics["avg_winner"] != 0 and metrics["avg_loser"] != 0:
        wl_ratio = abs(metrics["avg_winner"] / metrics["avg_loser"])
        if wl_ratio < 1.0:
            suggestions.append(
                f"Average winner (${metrics['avg_winner']:.2f}) is smaller than "
                f"average loser (${metrics['avg_loser']:.2f}). Widen profit "
                f"targets or tighten stop losses."
            )

    # Strategy-specific
    for strategy, sm in strategy_breakdown.items():
        if sm["total_trades"] >= 5 and sm["win_rate"] < 0.35:
            suggestions.append(
                f"Strategy '{strategy}' has {sm['win_rate']:.0%} win rate over "
                f"{sm['total_trades']} trades. Consider pausing this strategy "
                f"or reviewing its entry criteria."
            )

    # Drawdown
    if drawdown["current_drawdown_pct"] > 15:
        suggestions.append(
            f"Current drawdown is {drawdown['current_drawdown_pct']:.1f}%. "
            f"Approaching 20% stop-trading threshold. Reduce position sizes "
            f"by 50% immediately."
        )
    elif drawdown["max_drawdown_pct"] > 10:
        suggestions.append(
            f"Max drawdown reached {drawdown['max_drawdown_pct']:.1f}% this "
            f"period. Review whether position sizing is appropriate."
        )

    # Open position count
    if len(open_positions) >= 5:
        suggestions.append(
            f"Currently at {len(open_positions)} open positions (maximum). "
            f"Close existing positions before opening new ones."
        )

    # Hold time
    if metrics["avg_hold_time_hours"] > 48:
        suggestions.append(
            f"Average hold time is {metrics['avg_hold_time_hours']:.0f} hours. "
            f"Momentum and news-driven trades should exit within 15 minutes "
            f"to 48 hours. Review if time-based exits are being enforced."
        )

    if not suggestions:
        suggestions.append(
            "Performance looks healthy. Continue with current parameters. "
            "Review again after 20+ more trades for statistical significance."
        )

    return suggestions


def format_review(metrics, strategy_breakdown, drawdown, open_positions,
                  suggestions, days, trades):
    """Format the review as structured output."""
    output = {
        "review_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "period_days": days,
        "overall_metrics": metrics,
        "drawdown": drawdown,
        "strategy_breakdown": strategy_breakdown,
        "open_positions": len(open_positions),
        "suggestions": suggestions,
    }

    # Include the 5 most recent trades for context
    recent = []
    for t in trades[:5]:
        recent.append({
            "market": t.get("market_question", ""),
            "side": t.get("side", ""),
            "edge_type": t.get("edge_type", ""),
            "pnl": float(t.get("realized_pnl", 0)),
            "entry": float(t.get("entry_price", 0)),
            "exit": float(t.get("exit_price", 0)),
            "closed_at": t.get("closed_at", ""),
        })
    if recent:
        output["recent_trades"] = recent

    return output


def generate_sample_review():
    """Generate a sample review when no database is available."""
    return {
        "review_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "period_days": 1,
        "status": "no_database",
        "message": (
            "No paper trading database found. To generate a real performance "
            "review, first execute some paper trades using the "
            "polymarket-paper-trader skill. The database will be created at "
            "~/.polymarket-paper/portfolio.db."
        ),
        "overall_metrics": {
            "total_trades": 0,
            "winners": 0,
            "losers": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
        },
        "suggestions": [
            "Start by running: python polymarket-strategy-advisor/scripts/advisor.py --top 5",
            "Use the recommendations to place paper trades via the paper-trader skill.",
            "After 10+ trades, run this review again for meaningful analysis.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Analyze paper trading performance and suggest improvements"
    )
    parser.add_argument(
        "--portfolio-db", type=str, default=DEFAULT_DB_PATH,
        help=f"Path to paper trader SQLite database (default: {DEFAULT_DB_PATH})"
    )
    parser.add_argument(
        "--days", type=int, default=1,
        help="Number of days to review (default: 1)"
    )

    args = parser.parse_args()

    conn = connect_db(args.portfolio_db)
    if conn is None:
        print(json.dumps(generate_sample_review(), indent=2))
        return

    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    trades = get_closed_trades(conn, since)
    open_positions = get_open_positions(conn)
    account_history = get_account_history(conn, since)

    metrics = compute_metrics(trades)
    strategy_breakdown = breakdown_by_strategy(trades)
    drawdown = compute_drawdown(account_history)
    suggestions = generate_suggestions(
        metrics, strategy_breakdown, drawdown, open_positions
    )

    output = format_review(
        metrics, strategy_breakdown, drawdown, open_positions,
        suggestions, args.days, trades,
    )

    conn.close()
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
