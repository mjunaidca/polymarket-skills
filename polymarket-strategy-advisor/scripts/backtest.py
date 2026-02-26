#!/usr/bin/env python3
"""Backtesting engine for Polymarket paper trading strategies.

Reads historical trades from the paper trading database, replays closed
positions, marks open positions to market, and computes performance metrics
including Sharpe ratio, drawdown, profit factor, and per-strategy breakdowns.

Produces a live-readiness assessment against the CLAUDE.md prerequisites
(20+ closed trades, >55% win rate, >0.5 Sharpe, <15% max drawdown).

Usage:
    python backtest.py
    python backtest.py --portfolio-db ~/.polymarket-paper/portfolio.db
    python backtest.py --days 7 --json
    python backtest.py --live-check
"""

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = os.path.expanduser("~/.polymarket-paper/portfolio.db")
CLOB_API = "https://clob.polymarket.com"
RISK_FREE_RATE = 0.045  # 4.5% annualized
TRADING_DAYS_PER_YEAR = 365  # Prediction markets trade every day

# Live-readiness thresholds (from CLAUDE.md Section 4)
LIVE_MIN_CLOSED_TRADES = 20
LIVE_MIN_WIN_RATE = 0.55
LIVE_MIN_SHARPE = 0.5
LIVE_MAX_DRAWDOWN = 0.15

# CLAUDE.md risk limits (authoritative source of truth)
RISK_LIMITS = {
    "max_position_pct": 0.10,
    "max_position_pct_low_confidence": 0.05,
    "max_position_pct_news": 0.02,
    "max_position_pct_new_strategy": 0.01,
    "max_position_pct_arbitrage": 0.20,
    "min_trade_size_usd": 10.0,
    "max_concurrent_positions": 5,
    "max_single_market_pct": 0.20,
    "max_new_trades_per_day": 10,
    "daily_loss_limit_pct": 0.05,
    "weekly_loss_limit_pct": 0.10,
    "drawdown_reduce_50_pct": 0.10,
    "drawdown_reduce_75_pct": 0.15,
    "drawdown_halt_pct": 0.20,
}

# Strategy keywords to look for in the reasoning field
STRATEGY_KEYWORDS = {
    "arbitrage": ["arbitrage", "arb", "gabagool", "yes+no", "underpriced pair"],
    "momentum": ["momentum", "imbalance", "vol/liq", "volume/liquidity"],
    "mean-reversion": ["mean-reversion", "mean reversion", "spread", "midpoint",
                       "deviates", "revert"],
    "news": ["news", "breaking", "announcement", "event-driven", "headline"],
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _api_get(url, timeout=15):
    """GET JSON from a URL. Returns parsed JSON or None on failure."""
    req = Request(url, headers={"User-Agent": "polymarket-backtest/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (URLError, HTTPError, ValueError, OSError):
        return None


def fetch_midpoint(token_id):
    """Fetch the current midpoint price for a CLOB token.

    Returns the midpoint as a float, or None if the request fails.
    """
    data = _api_get(f"{CLOB_API}/midpoint?token_id={token_id}")
    if data and "mid" in data:
        try:
            return float(data["mid"])
        except (ValueError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def connect_db(db_path):
    """Open a read-only connection to the paper trader database.

    Returns None if the file does not exist.
    """
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_portfolio_id(conn, portfolio_name):
    """Return the active portfolio ID for the given name, or None."""
    row = conn.execute(
        "SELECT id, starting_balance, cash_balance, peak_value "
        "FROM portfolios WHERE name = ? AND active = 1 "
        "ORDER BY id DESC LIMIT 1",
        (portfolio_name,),
    ).fetchone()
    return dict(row) if row else None


def get_all_trades(conn, portfolio_id, since=None):
    """Fetch all trades for a portfolio, optionally filtered by date.

    Returns a list of dicts sorted by executed_at ascending.
    """
    if since:
        rows = conn.execute(
            "SELECT * FROM trades WHERE portfolio_id = ? AND executed_at >= ? "
            "ORDER BY executed_at ASC",
            (portfolio_id, since.isoformat()),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trades WHERE portfolio_id = ? "
            "ORDER BY executed_at ASC",
            (portfolio_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_positions(conn, portfolio_id):
    """Fetch all open positions for a portfolio."""
    rows = conn.execute(
        "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 0 "
        "ORDER BY opened_at ASC",
        (portfolio_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_closed_positions(conn, portfolio_id, since=None):
    """Fetch all closed positions for a portfolio."""
    if since:
        rows = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 1 "
            "AND closed_at >= ? ORDER BY closed_at ASC",
            (portfolio_id, since.isoformat()),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 1 "
            "ORDER BY closed_at ASC",
            (portfolio_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_daily_snapshots(conn, portfolio_id, since=None):
    """Fetch daily portfolio snapshots."""
    if since:
        rows = conn.execute(
            "SELECT * FROM daily_snapshots WHERE portfolio_id = ? "
            "AND date >= ? ORDER BY date ASC",
            (portfolio_id, since.strftime("%Y-%m-%d")),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM daily_snapshots WHERE portfolio_id = ? "
            "ORDER BY date ASC",
            (portfolio_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Trade pairing — match BUY entries with SELL/CLOSE exits
# ---------------------------------------------------------------------------

def classify_strategy(reasoning):
    """Classify a trade's strategy type from its reasoning text.

    Returns one of: 'arbitrage', 'momentum', 'mean-reversion', 'news',
    or 'unclassified'.
    """
    if not reasoning:
        return "unclassified"
    text = reasoning.lower()
    for strategy, keywords in STRATEGY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return strategy
    return "unclassified"


def pair_trades(trades):
    """Match BUY trades with corresponding SELL trades to form round trips.

    A round trip is a sequence of BUY(s) followed by SELL(s) for the same
    (token_id, side) pair. We use FIFO matching.

    Returns:
        closed_trips: list of dicts with entry/exit details and P&L
        open_entries: list of dicts for positions that were bought but never sold
    """
    # Accumulate buys per (token_id, side) key
    buy_queues = {}  # key -> list of {shares_remaining, price, reasoning, time}
    closed_trips = []

    for t in trades:
        key = (t["token_id"], t["side"])
        action = t["action"]

        if action == "BUY":
            if key not in buy_queues:
                buy_queues[key] = []
            buy_queues[key].append({
                "shares_remaining": t["shares"],
                "price": t["price"],
                "fee": t.get("fee", 0),
                "total_cost": t.get("total_cost", t["shares"] * t["price"]),
                "reasoning": t.get("reasoning", ""),
                "executed_at": t["executed_at"],
                "market_question": t.get("market_question", ""),
            })

        elif action in ("SELL", "CLOSE"):
            if key not in buy_queues or not buy_queues[key]:
                # Sell without a preceding buy — skip orphaned sell
                continue

            sell_shares = t["shares"]
            sell_price = t["price"]
            sell_fee = t.get("fee", 0)
            sell_time = t["executed_at"]

            # FIFO match against queued buys
            while sell_shares > 0.0001 and buy_queues[key]:
                buy = buy_queues[key][0]
                matched = min(sell_shares, buy["shares_remaining"])

                entry_price = buy["price"]
                # Proportional entry fee
                entry_fee = buy["fee"] * (matched / (buy["shares_remaining"]
                                          + (buy["total_cost"] / buy["price"]
                                             - buy["shares_remaining"])
                                          if buy["shares_remaining"] > 0 else 1))
                # Simplified: attribute fees proportionally
                if buy["shares_remaining"] > 0:
                    buy_fee_portion = buy["fee"] * (matched / buy["shares_remaining"])
                else:
                    buy_fee_portion = 0
                if t["shares"] > 0:
                    sell_fee_portion = sell_fee * (matched / t["shares"])
                else:
                    sell_fee_portion = 0

                pnl = (sell_price - entry_price) * matched - buy_fee_portion - sell_fee_portion
                cost_basis = entry_price * matched + buy_fee_portion

                # Calculate hold time
                hold_hours = 0
                try:
                    entry_dt = datetime.fromisoformat(buy["executed_at"])
                    exit_dt = datetime.fromisoformat(sell_time)
                    hold_hours = (exit_dt - entry_dt).total_seconds() / 3600
                except (ValueError, TypeError):
                    pass

                strategy = classify_strategy(buy["reasoning"])

                closed_trips.append({
                    "token_id": key[0],
                    "side": key[1],
                    "market_question": buy["market_question"],
                    "entry_price": entry_price,
                    "exit_price": sell_price,
                    "shares": round(matched, 4),
                    "cost_basis": round(cost_basis, 4),
                    "proceeds": round(sell_price * matched - sell_fee_portion, 4),
                    "pnl": round(pnl, 4),
                    "return_pct": round(pnl / cost_basis * 100, 2) if cost_basis > 0 else 0,
                    "entry_fee": round(buy_fee_portion, 4),
                    "exit_fee": round(sell_fee_portion, 4),
                    "entry_time": buy["executed_at"],
                    "exit_time": sell_time,
                    "hold_hours": round(hold_hours, 1),
                    "strategy": strategy,
                    "reasoning": buy["reasoning"],
                })

                buy["shares_remaining"] -= matched
                sell_shares -= matched

                if buy["shares_remaining"] < 0.0001:
                    buy_queues[key].pop(0)

    # Collect remaining open entries
    open_entries = []
    for key, buys in buy_queues.items():
        for buy in buys:
            if buy["shares_remaining"] > 0.0001:
                open_entries.append({
                    "token_id": key[0],
                    "side": key[1],
                    "market_question": buy["market_question"],
                    "entry_price": buy["price"],
                    "shares": round(buy["shares_remaining"], 4),
                    "cost_basis": round(buy["price"] * buy["shares_remaining"], 4),
                    "entry_time": buy["executed_at"],
                    "strategy": classify_strategy(buy["reasoning"]),
                    "reasoning": buy["reasoning"],
                })

    return closed_trips, open_entries


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def compute_core_metrics(closed_trips):
    """Compute core performance metrics from closed round trips.

    Returns a dict of metrics. Returns safe defaults for empty input.
    """
    if not closed_trips:
        return {
            "total_closed_trades": 0,
            "winners": 0,
            "losers": 0,
            "breakeven": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
            "largest_winner": 0.0,
            "largest_loser": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": 0.0,
            "avg_return_pct": 0.0,
            "avg_hold_hours": 0.0,
            "total_fees": 0.0,
        }

    pnls = [t["pnl"] for t in closed_trips]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    breakevens = [p for p in pnls if p == 0]
    hold_hours = [t["hold_hours"] for t in closed_trips if t["hold_hours"] > 0]
    returns = [t["return_pct"] for t in closed_trips]
    fees = [t["entry_fee"] + t["exit_fee"] for t in closed_trips]

    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))

    return {
        "total_closed_trades": len(closed_trips),
        "winners": len(winners),
        "losers": len(losers),
        "breakeven": len(breakevens),
        "win_rate": round(len(winners) / len(closed_trips), 4) if closed_trips else 0,
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(sum(pnls) / len(pnls), 2),
        "avg_winner": round(gross_profit / len(winners), 2) if winners else 0,
        "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
        "largest_winner": round(max(winners), 2) if winners else 0,
        "largest_loser": round(min(losers), 2) if losers else 0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else float("inf"),
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else 0,
        "avg_hold_hours": round(sum(hold_hours) / len(hold_hours), 1) if hold_hours else 0,
        "total_fees": round(sum(fees), 4),
    }


def compute_drawdown(snapshots, starting_balance):
    """Compute max drawdown and current drawdown from daily snapshots.

    If snapshots are empty, falls back to starting_balance as the only
    data point and reports zero drawdown.
    """
    if not snapshots:
        return {
            "max_drawdown_pct": 0.0,
            "max_drawdown_usd": 0.0,
            "current_drawdown_pct": 0.0,
            "current_drawdown_usd": 0.0,
            "peak_value": starting_balance,
            "trough_value": starting_balance,
            "peak_date": None,
            "trough_date": None,
        }

    values = [(s["date"], float(s["total_value"])) for s in snapshots]
    peak = values[0][1]
    peak_date = values[0][0]
    max_dd = 0.0
    max_dd_usd = 0.0
    trough_value = peak
    trough_date = peak_date

    for date, v in values:
        if v > peak:
            peak = v
            peak_date = date
        dd = (peak - v) / peak if peak > 0 else 0
        dd_usd = peak - v
        if dd > max_dd:
            max_dd = dd
            max_dd_usd = dd_usd
            trough_value = v
            trough_date = date

    current_value = values[-1][1]
    overall_peak = max(v for _, v in values)
    current_dd = (overall_peak - current_value) / overall_peak if overall_peak > 0 else 0
    current_dd_usd = overall_peak - current_value

    return {
        "max_drawdown_pct": round(max_dd * 100, 2),
        "max_drawdown_usd": round(max_dd_usd, 2),
        "current_drawdown_pct": round(current_dd * 100, 2),
        "current_drawdown_usd": round(current_dd_usd, 2),
        "peak_value": round(overall_peak, 2),
        "trough_value": round(trough_value, 2),
        "peak_date": peak_date,
        "trough_date": trough_date,
    }


def compute_sharpe_ratio(snapshots, starting_balance):
    """Compute annualized Sharpe ratio from daily portfolio values.

    Uses daily returns derived from consecutive snapshots. Falls back to
    using starting_balance + single snapshot if only one data point exists.

    Risk-free rate: 4.5% annualized (US Treasury).
    """
    if len(snapshots) < 2:
        return 0.0

    values = [float(s["total_value"]) for s in snapshots]
    daily_returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            daily_returns.append((values[i] - values[i - 1]) / values[i - 1])

    if not daily_returns:
        return 0.0

    daily_rf = RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
    excess_returns = [r - daily_rf for r in daily_returns]

    mean_excess = statistics.mean(excess_returns)
    if len(excess_returns) < 2:
        return 0.0

    std_dev = statistics.stdev(excess_returns)
    if std_dev == 0:
        return float("inf") if mean_excess > 0 else 0.0

    sharpe = (mean_excess / std_dev) * math.sqrt(TRADING_DAYS_PER_YEAR)
    return round(sharpe, 4)


def compute_total_return(starting_balance, current_value):
    """Compute total return as a percentage."""
    if starting_balance <= 0:
        return 0.0
    return round((current_value - starting_balance) / starting_balance * 100, 2)


# ---------------------------------------------------------------------------
# Mark-to-market for open positions
# ---------------------------------------------------------------------------

def mark_to_market(open_positions):
    """Fetch live prices for open positions and calculate unrealized P&L.

    Returns a list of position dicts with current_price and unrealized_pnl
    added, plus a summary dict.
    """
    marked = []
    total_unrealized = 0.0
    total_market_value = 0.0
    fetch_errors = 0

    for pos in open_positions:
        token_id = pos["token_id"]
        entry_price = pos["avg_entry"]
        shares = pos["shares"]

        live_price = fetch_midpoint(token_id)
        if live_price is not None:
            current_price = live_price
        else:
            # Fall back to the stored current_price from the DB
            current_price = pos.get("current_price", entry_price)
            fetch_errors += 1

        market_value = shares * current_price
        unrealized_pnl = (current_price - entry_price) * shares
        return_pct = ((current_price - entry_price) / entry_price * 100
                      if entry_price > 0 else 0)

        # Calculate hold time
        hold_hours = 0
        try:
            opened = datetime.fromisoformat(pos["opened_at"])
            now = datetime.now(timezone.utc)
            hold_hours = (now - opened).total_seconds() / 3600
        except (ValueError, TypeError):
            pass

        marked.append({
            "token_id": token_id,
            "side": pos["side"],
            "market_question": pos.get("market_question", ""),
            "shares": shares,
            "entry_price": entry_price,
            "current_price": round(current_price, 6),
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "return_pct": round(return_pct, 2),
            "hold_hours": round(hold_hours, 1),
            "price_source": "live" if live_price is not None else "cached",
        })
        total_unrealized += unrealized_pnl
        total_market_value += market_value

    summary = {
        "num_open": len(marked),
        "total_market_value": round(total_market_value, 2),
        "total_unrealized_pnl": round(total_unrealized, 2),
        "fetch_errors": fetch_errors,
    }

    return marked, summary


# ---------------------------------------------------------------------------
# Strategy breakdown
# ---------------------------------------------------------------------------

def compute_strategy_breakdown(closed_trips):
    """Break down metrics by strategy type.

    Returns a dict mapping strategy name to metrics dict.
    """
    by_strategy = {}
    for trip in closed_trips:
        strategy = trip["strategy"]
        if strategy not in by_strategy:
            by_strategy[strategy] = []
        by_strategy[strategy].append(trip)

    breakdown = {}
    for strategy, trips in sorted(by_strategy.items()):
        metrics = compute_core_metrics(trips)
        breakdown[strategy] = metrics

    return breakdown


# ---------------------------------------------------------------------------
# Risk rule compliance check
# ---------------------------------------------------------------------------

def check_risk_compliance(closed_trips, open_positions, snapshots,
                          starting_balance, portfolio_value):
    """Check historical trades against CLAUDE.md risk rules.

    Returns a list of violations found.
    """
    violations = []

    # Check max concurrent positions (from trades timeline)
    # This is a simplification — we check current open positions
    if len(open_positions) > RISK_LIMITS["max_concurrent_positions"]:
        violations.append({
            "rule": "max_concurrent_positions",
            "limit": RISK_LIMITS["max_concurrent_positions"],
            "actual": len(open_positions),
            "severity": "HIGH",
            "detail": (
                f"Currently {len(open_positions)} open positions, "
                f"limit is {RISK_LIMITS['max_concurrent_positions']}"
            ),
        })

    # Check drawdown thresholds
    if snapshots:
        values = [float(s["total_value"]) for s in snapshots]
        peak = max(values)
        current = values[-1]
        if peak > 0:
            dd = (peak - current) / peak
            if dd >= RISK_LIMITS["drawdown_halt_pct"]:
                violations.append({
                    "rule": "drawdown_halt",
                    "limit": f"{RISK_LIMITS['drawdown_halt_pct'] * 100:.0f}%",
                    "actual": f"{dd * 100:.1f}%",
                    "severity": "CRITICAL",
                    "detail": "Drawdown exceeds halt threshold. All trading should stop.",
                })
            elif dd >= RISK_LIMITS["drawdown_reduce_75_pct"]:
                violations.append({
                    "rule": "drawdown_reduce_75",
                    "limit": f"{RISK_LIMITS['drawdown_reduce_75_pct'] * 100:.0f}%",
                    "actual": f"{dd * 100:.1f}%",
                    "severity": "HIGH",
                    "detail": (
                        "Drawdown exceeds 15%. Position sizes should be reduced "
                        "by 75%. No new momentum or news trades."
                    ),
                })
            elif dd >= RISK_LIMITS["drawdown_reduce_50_pct"]:
                violations.append({
                    "rule": "drawdown_reduce_50",
                    "limit": f"{RISK_LIMITS['drawdown_reduce_50_pct'] * 100:.0f}%",
                    "actual": f"{dd * 100:.1f}%",
                    "severity": "MEDIUM",
                    "detail": "Drawdown exceeds 10%. All position sizes should be reduced by 50%.",
                })

    # Check per-trade size violations in historical trades
    oversized_count = 0
    for trip in closed_trips:
        if portfolio_value > 0:
            size_pct = trip["cost_basis"] / portfolio_value
            if size_pct > RISK_LIMITS["max_position_pct_arbitrage"]:
                oversized_count += 1

    if oversized_count > 0:
        violations.append({
            "rule": "position_sizing",
            "limit": f"{RISK_LIMITS['max_position_pct'] * 100:.0f}% default",
            "actual": f"{oversized_count} oversized trades",
            "severity": "MEDIUM",
            "detail": f"{oversized_count} trades exceeded the maximum position size cap.",
        })

    return violations


# ---------------------------------------------------------------------------
# Live-readiness assessment
# ---------------------------------------------------------------------------

def assess_live_readiness(core_metrics, sharpe, drawdown):
    """Check paper trading record against CLAUDE.md live prerequisites.

    Returns a dict with overall verdict and per-criterion results.
    """
    criteria = []

    # 1. Minimum closed trades
    closed_count = core_metrics["total_closed_trades"]
    passed_trades = closed_count >= LIVE_MIN_CLOSED_TRADES
    criteria.append({
        "criterion": "Closed trades >= 20",
        "required": LIVE_MIN_CLOSED_TRADES,
        "actual": closed_count,
        "passed": passed_trades,
        "gap": max(0, LIVE_MIN_CLOSED_TRADES - closed_count) if not passed_trades else 0,
    })

    # 2. Win rate
    win_rate = core_metrics["win_rate"]
    passed_wr = win_rate >= LIVE_MIN_WIN_RATE
    criteria.append({
        "criterion": "Win rate > 55%",
        "required": f"{LIVE_MIN_WIN_RATE * 100:.0f}%",
        "actual": f"{win_rate * 100:.1f}%",
        "passed": passed_wr,
        "gap": (
            f"{(LIVE_MIN_WIN_RATE - win_rate) * 100:.1f}pp short"
            if not passed_wr else "0"
        ),
    })

    # 3. Sharpe ratio
    passed_sharpe = sharpe >= LIVE_MIN_SHARPE
    criteria.append({
        "criterion": "Sharpe ratio > 0.5",
        "required": LIVE_MIN_SHARPE,
        "actual": round(sharpe, 4),
        "passed": passed_sharpe,
        "gap": round(LIVE_MIN_SHARPE - sharpe, 4) if not passed_sharpe else 0,
    })

    # 4. Max drawdown
    max_dd = drawdown["max_drawdown_pct"] / 100  # Convert from percentage
    passed_dd = max_dd < LIVE_MAX_DRAWDOWN
    criteria.append({
        "criterion": "Max drawdown < 15%",
        "required": f"{LIVE_MAX_DRAWDOWN * 100:.0f}%",
        "actual": f"{max_dd * 100:.1f}%",
        "passed": passed_dd,
        "gap": (
            f"{(max_dd - LIVE_MAX_DRAWDOWN) * 100:.1f}pp over"
            if not passed_dd else "0"
        ),
    })

    all_passed = all(c["passed"] for c in criteria)
    passed_count = sum(1 for c in criteria if c["passed"])

    verdict = "READY" if all_passed else "NOT READY"

    # Build specific gap descriptions
    gaps = []
    if not passed_trades:
        gaps.append(
            f"Need {LIVE_MIN_CLOSED_TRADES - closed_count} more closed trades "
            f"(have {closed_count}, need {LIVE_MIN_CLOSED_TRADES})"
        )
    if not passed_wr:
        gaps.append(
            f"Win rate {win_rate * 100:.1f}% is below {LIVE_MIN_WIN_RATE * 100:.0f}% "
            f"threshold. Review entry criteria and stop-loss discipline."
        )
    if not passed_sharpe:
        gaps.append(
            f"Sharpe ratio {sharpe:.2f} is below {LIVE_MIN_SHARPE} minimum. "
            f"Improve consistency of returns or reduce variance."
        )
    if not passed_dd:
        gaps.append(
            f"Max drawdown {max_dd * 100:.1f}% exceeds {LIVE_MAX_DRAWDOWN * 100:.0f}% "
            f"limit. Tighten position sizing and stop-loss rules."
        )

    return {
        "verdict": verdict,
        "criteria_passed": passed_count,
        "criteria_total": len(criteria),
        "criteria": criteria,
        "gaps": gaps,
        "recommendation": (
            "Paper trading record meets all CLAUDE.md prerequisites for live "
            "trading. Start with First-Time tier: $25 max wallet, $5 max per "
            "trade, $10 daily loss limit."
            if all_passed else
            "Continue paper trading until all criteria are met. "
            "Do not go live with unmet prerequisites."
        ),
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_human_readable(result):
    """Format the full backtest result as human-readable text."""
    lines = []

    lines.append("=" * 70)
    lines.append("  POLYMARKET PAPER TRADING BACKTEST REPORT")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"  Generated: {result['generated_at']}")
    lines.append(f"  Portfolio: {result['portfolio_name']}")
    if result.get("period_days"):
        lines.append(f"  Period:    Last {result['period_days']} days")
    else:
        lines.append(f"  Period:    All time")
    lines.append("")

    # --- Portfolio Summary ---
    ps = result["portfolio_summary"]
    lines.append("-" * 70)
    lines.append("  PORTFOLIO SUMMARY")
    lines.append("-" * 70)
    lines.append(f"  Starting Balance:    ${ps['starting_balance']:>12,.2f}")
    lines.append(f"  Current Value:       ${ps['current_value']:>12,.2f}")
    lines.append(f"  Cash Balance:        ${ps['cash_balance']:>12,.2f}")
    lines.append(f"  Positions Value:     ${ps['positions_value']:>12,.2f}")
    lines.append(f"  Total Return:        {ps['total_return_pct']:>12.2f}%")
    lines.append("")

    # --- Core Metrics ---
    m = result["core_metrics"]
    lines.append("-" * 70)
    lines.append("  PERFORMANCE METRICS")
    lines.append("-" * 70)
    lines.append(f"  Total Closed Trades: {m['total_closed_trades']:>8d}")
    lines.append(f"  Open Positions:      {result['open_positions_summary']['num_open']:>8d}")
    lines.append(f"  Winners:             {m['winners']:>8d}")
    lines.append(f"  Losers:              {m['losers']:>8d}")
    lines.append(f"  Breakeven:           {m['breakeven']:>8d}")
    lines.append(f"  Win Rate:            {m['win_rate'] * 100:>8.1f}%")
    lines.append(f"  Total P&L:           ${m['total_pnl']:>12,.2f}")
    lines.append(f"  Avg P&L/Trade:       ${m['avg_pnl']:>12,.2f}")
    lines.append(f"  Avg Winner:          ${m['avg_winner']:>12,.2f}")
    lines.append(f"  Avg Loser:           ${m['avg_loser']:>12,.2f}")
    lines.append(f"  Largest Winner:      ${m['largest_winner']:>12,.2f}")
    lines.append(f"  Largest Loser:       ${m['largest_loser']:>12,.2f}")
    pf_str = f"{m['profit_factor']:.4f}" if m['profit_factor'] != float("inf") else "INF"
    lines.append(f"  Profit Factor:       {pf_str:>12s}")
    lines.append(f"  Total Fees:          ${m['total_fees']:>12,.4f}")
    lines.append(f"  Avg Hold Time:       {m['avg_hold_hours']:>8.1f} hours")
    lines.append("")

    # --- Risk Metrics ---
    dd = result["drawdown"]
    lines.append("-" * 70)
    lines.append("  RISK METRICS")
    lines.append("-" * 70)
    lines.append(f"  Max Drawdown:        {dd['max_drawdown_pct']:>8.2f}%  (${dd['max_drawdown_usd']:>,.2f})")
    lines.append(f"  Current Drawdown:    {dd['current_drawdown_pct']:>8.2f}%  (${dd['current_drawdown_usd']:>,.2f})")
    lines.append(f"  Peak Value:          ${dd['peak_value']:>12,.2f}")
    sharpe = result["sharpe_ratio"]
    sharpe_str = f"{sharpe:.4f}" if sharpe != float("inf") else "INF"
    lines.append(f"  Sharpe Ratio:        {sharpe_str:>12s}  (annualized, rf=4.5%)")
    lines.append("")

    # --- Open Positions ---
    ops = result["open_positions_summary"]
    if ops["num_open"] > 0:
        lines.append("-" * 70)
        lines.append("  OPEN POSITIONS (marked to market)")
        lines.append("-" * 70)
        for p in result.get("open_positions_detail", []):
            pnl_sign = "+" if p["unrealized_pnl"] >= 0 else ""
            lines.append(
                f"  {p['side']:>3} {p['shares']:>10.2f} sh @ "
                f"${p['entry_price']:.4f} -> ${p['current_price']:.4f}  "
                f"P&L: {pnl_sign}${p['unrealized_pnl']:,.2f} "
                f"({pnl_sign}{p['return_pct']:.1f}%)  "
                f"[{p['price_source']}]"
            )
            q = p.get("market_question", "")
            if q:
                lines.append(f"      {q[:65]}")
        lines.append(
            f"\n  Total Unrealized P&L: ${ops['total_unrealized_pnl']:+,.2f}  "
            f"Market Value: ${ops['total_market_value']:,.2f}"
        )
        if ops["fetch_errors"] > 0:
            lines.append(
                f"  ({ops['fetch_errors']} price fetch errors, using cached prices)"
            )
        lines.append("")

    # --- Strategy Breakdown ---
    sb = result.get("strategy_breakdown", {})
    if sb:
        lines.append("-" * 70)
        lines.append("  STRATEGY BREAKDOWN")
        lines.append("-" * 70)
        header = (
            f"  {'Strategy':<18s} {'Trades':>6s} {'Win%':>7s} "
            f"{'P&L':>10s} {'Avg P&L':>10s} {'PF':>8s}"
        )
        lines.append(header)
        lines.append("  " + "-" * 61)
        for strategy, sm in sb.items():
            pf_str = f"{sm['profit_factor']:.2f}" if sm['profit_factor'] != float("inf") else "INF"
            lines.append(
                f"  {strategy:<18s} {sm['total_closed_trades']:>6d} "
                f"{sm['win_rate'] * 100:>6.1f}% "
                f"${sm['total_pnl']:>9,.2f} "
                f"${sm['avg_pnl']:>9,.2f} "
                f"{pf_str:>8s}"
            )
        lines.append("")

    # --- Risk Violations ---
    violations = result.get("risk_violations", [])
    if violations:
        lines.append("-" * 70)
        lines.append("  RISK RULE VIOLATIONS")
        lines.append("-" * 70)
        for v in violations:
            lines.append(f"  [{v['severity']}] {v['rule']}: {v['detail']}")
        lines.append("")

    # --- Live Readiness ---
    lr = result["live_readiness"]
    lines.append("-" * 70)
    verdict_label = lr["verdict"]
    lines.append(f"  LIVE-READINESS ASSESSMENT: {verdict_label}")
    lines.append(f"  ({lr['criteria_passed']}/{lr['criteria_total']} criteria met)")
    lines.append("-" * 70)
    for c in lr["criteria"]:
        status = "PASS" if c["passed"] else "FAIL"
        lines.append(
            f"  [{status}] {c['criterion']:<25s}  "
            f"required: {str(c['required']):<8s}  "
            f"actual: {str(c['actual']):<10s}"
        )
    if lr["gaps"]:
        lines.append("")
        lines.append("  Gaps to close:")
        for g in lr["gaps"]:
            lines.append(f"    - {g}")
    lines.append("")
    lines.append(f"  {lr['recommendation']}")
    lines.append("")
    lines.append("=" * 70)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main backtest pipeline
# ---------------------------------------------------------------------------

def run_backtest(db_path, portfolio_name="default", days=None):
    """Execute the full backtest pipeline.

    Returns a structured dict with all metrics and assessments.
    """
    conn = connect_db(db_path)
    if conn is None:
        return {
            "error": f"Database not found: {db_path}",
            "suggestion": (
                "Run paper trades first using the polymarket-paper-trader skill. "
                "The database will be created at ~/.polymarket-paper/portfolio.db"
            ),
        }

    # Get portfolio metadata
    pf = get_portfolio_id(conn, portfolio_name)
    if pf is None:
        conn.close()
        return {
            "error": f"No active portfolio named '{portfolio_name}'",
            "suggestion": (
                "Initialize a portfolio with: python paper_engine.py --action init"
            ),
        }

    portfolio_id = pf["id"]
    starting_balance = pf["starting_balance"]
    cash_balance = pf["cash_balance"]

    # Determine date range
    since = None
    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)

    # Load data
    all_trades = get_all_trades(conn, portfolio_id, since)
    open_positions = get_open_positions(conn, portfolio_id)
    snapshots = get_daily_snapshots(conn, portfolio_id, since)

    conn.close()

    # Pair trades into round trips
    closed_trips, open_entries = pair_trades(all_trades)

    # Core metrics on closed trades
    core_metrics = compute_core_metrics(closed_trips)

    # Mark open positions to market
    marked_positions, open_summary = mark_to_market(open_positions)

    # Current portfolio value
    positions_value = open_summary["total_market_value"]
    current_value = cash_balance + positions_value

    # Total return
    total_return_pct = compute_total_return(starting_balance, current_value)

    # Drawdown
    drawdown = compute_drawdown(snapshots, starting_balance)

    # Sharpe ratio
    sharpe = compute_sharpe_ratio(snapshots, starting_balance)

    # Strategy breakdown
    strategy_breakdown = compute_strategy_breakdown(closed_trips)

    # Risk compliance check
    risk_violations = check_risk_compliance(
        closed_trips, open_positions, snapshots,
        starting_balance, current_value,
    )

    # Live readiness assessment
    live_readiness = assess_live_readiness(core_metrics, sharpe, drawdown)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "portfolio_name": portfolio_name,
        "period_days": days,
        "portfolio_summary": {
            "starting_balance": starting_balance,
            "cash_balance": round(cash_balance, 2),
            "positions_value": round(positions_value, 2),
            "current_value": round(current_value, 2),
            "total_return_pct": total_return_pct,
        },
        "core_metrics": core_metrics,
        "open_positions_summary": open_summary,
        "open_positions_detail": marked_positions,
        "drawdown": drawdown,
        "sharpe_ratio": sharpe,
        "strategy_breakdown": strategy_breakdown,
        "risk_violations": risk_violations,
        "live_readiness": live_readiness,
        "trade_counts": {
            "total_trades": len(all_trades),
            "closed_round_trips": len(closed_trips),
            "open_entries": len(open_entries),
            "open_positions": len(open_positions),
        },
    }

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backtest Polymarket paper trading strategies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --portfolio-db ~/.polymarket-paper/portfolio.db
  %(prog)s --days 7
  %(prog)s --json
  %(prog)s --live-check
  %(prog)s --portfolio mytest --days 30 --json
        """,
    )
    parser.add_argument(
        "--portfolio-db", type=str, default=DEFAULT_DB_PATH,
        help=f"Path to paper trader SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--portfolio", type=str, default="default",
        help="Portfolio name to analyze (default: 'default')",
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Analyze only the last N days (default: all time)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON instead of human-readable text",
    )
    parser.add_argument(
        "--live-check", action="store_true",
        help="Only show the live-readiness assessment",
    )

    args = parser.parse_args()

    result = run_backtest(
        db_path=args.portfolio_db,
        portfolio_name=args.portfolio,
        days=args.days,
    )

    # Handle errors
    if "error" in result:
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"ERROR: {result['error']}", file=sys.stderr)
            print(f"Suggestion: {result['suggestion']}", file=sys.stderr)
        sys.exit(1)

    # Live-check only mode
    if args.live_check:
        lr = result["live_readiness"]
        if args.json:
            print(json.dumps(lr, indent=2))
        else:
            verdict = lr["verdict"]
            print(f"\nLive-Readiness Assessment: {verdict}")
            print(f"({lr['criteria_passed']}/{lr['criteria_total']} criteria met)\n")
            for c in lr["criteria"]:
                status = "PASS" if c["passed"] else "FAIL"
                print(
                    f"  [{status}] {c['criterion']:<25s}  "
                    f"required: {str(c['required']):<8s}  "
                    f"actual: {str(c['actual'])}"
                )
            if lr["gaps"]:
                print("\nGaps:")
                for g in lr["gaps"]:
                    print(f"  - {g}")
            print(f"\n{lr['recommendation']}")
        return

    # Full output
    if args.json:
        # Convert inf to string for JSON serialization
        def sanitize(obj):
            if isinstance(obj, float):
                if math.isinf(obj):
                    return "Infinity" if obj > 0 else "-Infinity"
                if math.isnan(obj):
                    return "NaN"
            if isinstance(obj, dict):
                return {k: sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [sanitize(v) for v in obj]
            return obj

        print(json.dumps(sanitize(result), indent=2))
    else:
        print(format_human_readable(result))


if __name__ == "__main__":
    main()
