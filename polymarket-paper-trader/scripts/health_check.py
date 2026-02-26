#!/usr/bin/env python3
"""
Portfolio Health Check — Automated Session Start Workflow

Performs the complete CLAUDE.md Session Start sequence in one command:
1. Load portfolio from SQLite
2. Fetch LIVE prices from CLOB API for every open position
3. Update current_price in DB
4. Calculate per-position P&L and stop-loss status
5. Calculate portfolio-level metrics (value, drawdown, daily P&L, concentration)
6. Check graduated drawdown thresholds (10% / 15% / 20%)
7. Check daily loss limit (5%) and weekly loss limit (10%)
8. Output a clean summary with overall status: GREEN / YELLOW / RED
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Import paper_engine for DB access and API helpers
# ---------------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.append(_THIS_DIR)
from paper_engine import (
    DB_PATH,
    CLOB_API,
    _get_db,
    _active_portfolio,
    _validate_token_id,
    _api_get,
    fetch_midpoint,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORTFOLIO_NAME = "default"
MAX_CONCURRENT_POSITIONS = 5
MAX_SINGLE_MARKET_PCT = 0.20  # 20%
DAILY_LOSS_LIMIT_PCT = 0.05   # 5%
WEEKLY_LOSS_LIMIT_PCT = 0.10  # 10%
DEFAULT_TRAILING_STOP_PCT = 0.15  # 15% trailing stop from entry

# Graduated drawdown thresholds from CLAUDE.md Section 2
DRAWDOWN_THRESHOLDS = [
    {
        "level": 0.10,
        "tier": "WARN",
        "label": "10% drawdown",
        "action": "Reduce ALL position sizes by 50%",
        "restrictions": None,
    },
    {
        "level": 0.15,
        "tier": "ALERT",
        "label": "15% drawdown",
        "action": "Reduce ALL position sizes by 75%; no new momentum or news trades",
        "restrictions": ["no_momentum", "no_news"],
    },
    {
        "level": 0.20,
        "tier": "CRITICAL",
        "label": "20% drawdown",
        "action": "Close ALL positions; halt all trading; full strategy review required",
        "restrictions": ["halt_all"],
    },
]


# ---------------------------------------------------------------------------
# Live price fetching
# ---------------------------------------------------------------------------

def fetch_live_price(token_id: str) -> float | None:
    """
    Fetch the midpoint price for a token from the CLOB API.
    Returns None on failure instead of raising.
    """
    try:
        return fetch_midpoint(token_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core health check logic
# ---------------------------------------------------------------------------

def run_health_check(
    db_path: str | Path = DB_PATH,
    portfolio_name: str = DEFAULT_PORTFOLIO_NAME,
) -> dict:
    """
    Execute the full Session Start health check workflow.

    Returns a structured dict containing:
    - portfolio overview
    - per-position details with live prices and stop-loss status
    - risk utilization metrics
    - alerts/warnings
    - overall status (GREEN / YELLOW / RED)
    """
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        raise RuntimeError(
            f"Portfolio database not found: {db_path}\n"
            f"Run: python paper_engine.py --action init"
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        # ---------------------------------------------------------------
        # 1. Load portfolio
        # ---------------------------------------------------------------
        pf = _active_portfolio(conn, portfolio_name)
        pid = pf["id"]
        starting_balance = pf["starting_balance"]
        cash_balance = pf["cash_balance"]
        peak_value = pf["peak_value"]

        # ---------------------------------------------------------------
        # 2-3. Fetch live prices and update DB for each open position
        # ---------------------------------------------------------------
        positions_rows = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 0",
            (pid,),
        ).fetchall()

        now_iso = datetime.now(timezone.utc).isoformat()
        positions = []
        price_errors = []

        for row in positions_rows:
            p = dict(row)
            token_id = p["token_id"]

            live_price = fetch_live_price(token_id)
            if live_price is not None:
                p["current_price"] = live_price
                conn.execute(
                    "UPDATE positions SET current_price = ?, updated_at = ? WHERE id = ?",
                    (live_price, now_iso, p["id"]),
                )
            else:
                price_errors.append(token_id)
                # Keep stale price from DB

            positions.append(p)

        conn.commit()

        # ---------------------------------------------------------------
        # 4. Per-position P&L and stop-loss status
        # ---------------------------------------------------------------
        position_details = []
        positions_value = 0.0

        for p in positions:
            shares = p["shares"]
            entry = p["avg_entry"]
            current = p["current_price"]
            value = shares * current
            unrealized_pnl = (current - entry) * shares
            pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0.0

            # Stop-loss: default 15% trailing from entry
            # (CLAUDE.md says stop_loss = entry_price - edge/2, but we
            # don't store edge, so use 15% trailing stop from entry)
            stop_price = entry * (1 - DEFAULT_TRAILING_STOP_PCT)
            stop_triggered = current <= stop_price

            position_details.append({
                "id": p["id"],
                "token_id": p["token_id"],
                "market_question": p["market_question"] or "Unknown",
                "side": p["side"],
                "shares": round(shares, 4),
                "avg_entry": round(entry, 6),
                "current_price": round(current, 6),
                "value": round(value, 4),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "stop_price": round(stop_price, 6),
                "stop_triggered": stop_triggered,
                "price_stale": p["token_id"] in price_errors,
                "opened_at": p["opened_at"],
            })

            positions_value += value

        # ---------------------------------------------------------------
        # 5. Portfolio-level metrics
        # ---------------------------------------------------------------
        total_value = cash_balance + positions_value
        overall_pnl = total_value - starting_balance
        overall_pnl_pct = (overall_pnl / starting_balance * 100) if starting_balance > 0 else 0.0

        # Update peak if we have a new high
        if total_value > peak_value:
            peak_value = total_value
            conn.execute(
                "UPDATE portfolios SET peak_value = ?, updated_at = ? WHERE id = ?",
                (peak_value, now_iso, pid),
            )
            conn.commit()

        # Drawdown from peak
        drawdown_usd = peak_value - total_value
        drawdown_pct = (drawdown_usd / peak_value * 100) if peak_value > 0 else 0.0

        # Daily P&L: compare to yesterday's snapshot or starting balance
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prev_snapshot = conn.execute(
            """SELECT total_value FROM daily_snapshots
               WHERE portfolio_id = ? AND date < ?
               ORDER BY date DESC LIMIT 1""",
            (pid, today),
        ).fetchone()
        prev_value = prev_snapshot["total_value"] if prev_snapshot else starting_balance
        daily_pnl = total_value - prev_value
        daily_pnl_pct = (daily_pnl / prev_value * 100) if prev_value > 0 else 0.0

        # Position count
        num_positions = len(position_details)

        # Single market concentration: max exposure to any one token_id
        exposure_by_token = {}
        for p in position_details:
            tid = p["token_id"]
            exposure_by_token[tid] = exposure_by_token.get(tid, 0.0) + p["value"]

        max_concentration = 0.0
        max_concentration_market = ""
        for tid, exp in exposure_by_token.items():
            pct = (exp / total_value) if total_value > 0 else 0.0
            if pct > max_concentration:
                max_concentration = pct
                # Look up market name
                for p in position_details:
                    if p["token_id"] == tid:
                        max_concentration_market = p["market_question"]
                        break

        # ---------------------------------------------------------------
        # 6. Graduated drawdown thresholds
        # ---------------------------------------------------------------
        drawdown_fraction = drawdown_pct / 100.0
        drawdown_tier = "NONE"
        drawdown_action = None

        for threshold in DRAWDOWN_THRESHOLDS:
            if drawdown_fraction >= threshold["level"]:
                drawdown_tier = threshold["tier"]
                drawdown_action = threshold["action"]

        # ---------------------------------------------------------------
        # 7. Daily and weekly loss limits
        # ---------------------------------------------------------------

        # Daily realized losses from SELL/CLOSE trades today
        daily_realized_row = conn.execute(
            """SELECT COALESCE(SUM(
                CASE WHEN action IN ('SELL','CLOSE') AND entry_avg IS NOT NULL
                     THEN (price - entry_avg) * shares
                     ELSE 0 END
            ), 0) as realized
            FROM trades
            WHERE portfolio_id = ? AND date(executed_at) = ?""",
            (pid, today),
        ).fetchone()
        daily_realized = daily_realized_row["realized"] if daily_realized_row else 0.0
        daily_loss = abs(min(0, daily_realized))
        daily_loss_limit = starting_balance * DAILY_LOSS_LIMIT_PCT
        daily_loss_breached = daily_loss >= daily_loss_limit

        # Weekly realized losses (since last Monday)
        now_dt = datetime.now(timezone.utc)
        days_since_monday = now_dt.weekday()  # Monday=0
        last_monday = (now_dt - timedelta(days=days_since_monday)).strftime("%Y-%m-%d")

        weekly_realized_row = conn.execute(
            """SELECT COALESCE(SUM(
                CASE WHEN action IN ('SELL','CLOSE') AND entry_avg IS NOT NULL
                     THEN (price - entry_avg) * shares
                     ELSE 0 END
            ), 0) as realized
            FROM trades
            WHERE portfolio_id = ? AND date(executed_at) >= ?""",
            (pid, last_monday),
        ).fetchone()
        weekly_realized = weekly_realized_row["realized"] if weekly_realized_row else 0.0
        weekly_loss = abs(min(0, weekly_realized))
        weekly_loss_limit = starting_balance * WEEKLY_LOSS_LIMIT_PCT
        weekly_loss_breached = weekly_loss >= weekly_loss_limit

        # ---------------------------------------------------------------
        # 8. Build alerts and determine overall status
        # ---------------------------------------------------------------
        alerts = []
        overall_status = "GREEN"

        # Stop-loss alerts
        stops_triggered = [p for p in position_details if p["stop_triggered"]]
        for p in stops_triggered:
            alerts.append({
                "severity": "HIGH",
                "type": "STOP_LOSS",
                "message": (
                    f"Stop-loss triggered for {p['side']} position in "
                    f"'{p['market_question'][:60]}': "
                    f"current ${p['current_price']:.4f} <= stop ${p['stop_price']:.4f}"
                ),
            })
            overall_status = "RED"

        # Stale price warnings
        if price_errors:
            alerts.append({
                "severity": "MEDIUM",
                "type": "STALE_PRICE",
                "message": (
                    f"Failed to fetch live prices for {len(price_errors)} position(s). "
                    f"Using stale cached prices."
                ),
            })
            if overall_status == "GREEN":
                overall_status = "YELLOW"

        # Drawdown alerts
        if drawdown_tier == "WARN":
            alerts.append({
                "severity": "MEDIUM",
                "type": "DRAWDOWN_WARN",
                "message": (
                    f"Drawdown at {drawdown_pct:.1f}% from peak. "
                    f"Recommendation: {drawdown_action}"
                ),
            })
            if overall_status == "GREEN":
                overall_status = "YELLOW"
        elif drawdown_tier == "ALERT":
            alerts.append({
                "severity": "HIGH",
                "type": "DRAWDOWN_ALERT",
                "message": (
                    f"Drawdown at {drawdown_pct:.1f}% from peak. "
                    f"Recommendation: {drawdown_action}"
                ),
            })
            overall_status = "RED"
        elif drawdown_tier == "CRITICAL":
            alerts.append({
                "severity": "CRITICAL",
                "type": "DRAWDOWN_CRITICAL",
                "message": (
                    f"Drawdown at {drawdown_pct:.1f}% from peak. "
                    f"REQUIRED ACTION: {drawdown_action}"
                ),
            })
            overall_status = "RED"

        # Daily loss limit
        if daily_loss_breached:
            alerts.append({
                "severity": "HIGH",
                "type": "DAILY_LOSS_LIMIT",
                "message": (
                    f"Daily loss limit breached: ${daily_loss:.2f} realized losses "
                    f"(limit: ${daily_loss_limit:.2f} = {DAILY_LOSS_LIMIT_PCT*100:.0f}% "
                    f"of starting balance). All new entries blocked until next UTC day."
                ),
            })
            overall_status = "RED"

        # Weekly loss limit
        if weekly_loss_breached:
            alerts.append({
                "severity": "HIGH",
                "type": "WEEKLY_LOSS_LIMIT",
                "message": (
                    f"Weekly loss limit breached: ${weekly_loss:.2f} realized losses "
                    f"(limit: ${weekly_loss_limit:.2f} = {WEEKLY_LOSS_LIMIT_PCT*100:.0f}% "
                    f"of starting balance). All new entries blocked until next Monday."
                ),
            })
            overall_status = "RED"

        # Concentration warning
        if max_concentration > MAX_SINGLE_MARKET_PCT:
            alerts.append({
                "severity": "MEDIUM",
                "type": "CONCENTRATION",
                "message": (
                    f"Single market concentration at {max_concentration*100:.1f}% "
                    f"(limit: {MAX_SINGLE_MARKET_PCT*100:.0f}%) in "
                    f"'{max_concentration_market[:60]}'"
                ),
            })
            if overall_status == "GREEN":
                overall_status = "YELLOW"

        # Position count warning
        if num_positions >= MAX_CONCURRENT_POSITIONS:
            alerts.append({
                "severity": "MEDIUM",
                "type": "MAX_POSITIONS",
                "message": (
                    f"At maximum concurrent positions: "
                    f"{num_positions}/{MAX_CONCURRENT_POSITIONS}. "
                    f"No new positions allowed."
                ),
            })
            if overall_status == "GREEN":
                overall_status = "YELLOW"

        # ---------------------------------------------------------------
        # Assemble result
        # ---------------------------------------------------------------
        result = {
            "status": overall_status,
            "checked_at": now_iso,
            "portfolio": {
                "name": portfolio_name,
                "starting_balance": round(starting_balance, 2),
                "cash_balance": round(cash_balance, 2),
                "positions_value": round(positions_value, 2),
                "total_value": round(total_value, 2),
                "pnl": round(overall_pnl, 2),
                "pnl_pct": round(overall_pnl_pct, 2),
                "peak_value": round(peak_value, 2),
                "drawdown_usd": round(drawdown_usd, 2),
                "drawdown_pct": round(drawdown_pct, 2),
                "daily_pnl": round(daily_pnl, 2),
                "daily_pnl_pct": round(daily_pnl_pct, 2),
            },
            "positions": position_details,
            "risk": {
                "position_count": num_positions,
                "position_limit": MAX_CONCURRENT_POSITIONS,
                "position_utilization": f"{num_positions}/{MAX_CONCURRENT_POSITIONS}",
                "max_concentration_pct": round(max_concentration * 100, 1),
                "max_concentration_market": max_concentration_market[:70] if max_concentration_market else "N/A",
                "concentration_limit_pct": MAX_SINGLE_MARKET_PCT * 100,
                "drawdown_tier": drawdown_tier,
                "drawdown_action": drawdown_action,
                "daily_loss": round(daily_loss, 2),
                "daily_loss_limit": round(daily_loss_limit, 2),
                "daily_loss_pct": round(daily_loss / starting_balance * 100, 2) if starting_balance > 0 else 0.0,
                "daily_loss_breached": daily_loss_breached,
                "weekly_loss": round(weekly_loss, 2),
                "weekly_loss_limit": round(weekly_loss_limit, 2),
                "weekly_loss_pct": round(weekly_loss / starting_balance * 100, 2) if starting_balance > 0 else 0.0,
                "weekly_loss_breached": weekly_loss_breached,
                "stops_triggered": len(stops_triggered),
            },
            "alerts": alerts,
        }

        return result

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Text formatting
# ---------------------------------------------------------------------------

def format_human_readable(result: dict) -> str:
    """Format the health check result as a human-readable report."""
    pf = result["portfolio"]
    risk = result["risk"]
    status = result["status"]

    # Status indicator
    status_bar = {
        "GREEN": "[GREEN]  All systems nominal",
        "YELLOW": "[YELLOW] Caution -- review warnings below",
        "RED": "[RED]    Action required -- review alerts below",
    }

    lines = [
        "=" * 64,
        f"  PORTFOLIO HEALTH CHECK",
        f"  {result['checked_at'][:19]} UTC",
        f"  Status: {status_bar.get(status, status)}",
        "=" * 64,
        "",
        "--- Portfolio Overview ---",
        f"  Starting Balance:   ${pf['starting_balance']:>12,.2f}",
        f"  Cash:               ${pf['cash_balance']:>12,.2f}",
        f"  Positions Value:    ${pf['positions_value']:>12,.2f}",
        f"  Total Value:        ${pf['total_value']:>12,.2f}",
        f"  P&L:                ${pf['pnl']:>12,.2f} ({pf['pnl_pct']:+.2f}%)",
        f"  Peak Value:         ${pf['peak_value']:>12,.2f}",
        f"  Drawdown:           ${pf['drawdown_usd']:>12,.2f} ({pf['drawdown_pct']:.2f}%)",
        f"  Daily P&L:          ${pf['daily_pnl']:>12,.2f} ({pf['daily_pnl_pct']:+.2f}%)",
    ]

    # --- Positions ---
    if result["positions"]:
        lines.append("")
        lines.append("--- Open Positions ---")
        lines.append(
            f"  {'Side':>4} {'Shares':>8} {'Entry':>8} {'Current':>8} "
            f"{'P&L':>10} {'P&L%':>7} {'Stop':>8} {'Status':>8}"
        )
        lines.append("  " + "-" * 74)

        for p in result["positions"]:
            stop_status = "STOP!" if p["stop_triggered"] else "OK"
            stale = " [stale]" if p["price_stale"] else ""
            lines.append(
                f"  {p['side']:>4} {p['shares']:>8.2f} "
                f"${p['avg_entry']:>.4f} ${p['current_price']:>.4f} "
                f"${p['unrealized_pnl']:>+9,.2f} {p['pnl_pct']:>+6.1f}% "
                f"${p['stop_price']:>.4f} {stop_status:>8}{stale}"
            )
            # Market question on its own line
            lines.append(f"       {p['market_question'][:60]}")
    else:
        lines.append("")
        lines.append("--- Open Positions ---")
        lines.append("  No open positions.")

    # --- Risk Utilization ---
    lines.append("")
    lines.append("--- Risk Utilization ---")
    lines.append(
        f"  Positions:          {risk['position_utilization']:>12}"
    )
    lines.append(
        f"  Max Concentration:  {risk['max_concentration_pct']:>11.1f}% "
        f"(limit: {risk['concentration_limit_pct']:.0f}%)"
    )
    if risk["max_concentration_market"] != "N/A":
        lines.append(
            f"    in: {risk['max_concentration_market']}"
        )
    lines.append(
        f"  Drawdown Tier:      {risk['drawdown_tier']:>12}"
    )
    if risk["drawdown_action"]:
        lines.append(f"    Action: {risk['drawdown_action']}")
    lines.append(
        f"  Daily Loss:         ${risk['daily_loss']:>11,.2f} / "
        f"${risk['daily_loss_limit']:,.2f} "
        f"({risk['daily_loss_pct']:.1f}% / {DAILY_LOSS_LIMIT_PCT*100:.0f}%)"
    )
    lines.append(
        f"  Weekly Loss:        ${risk['weekly_loss']:>11,.2f} / "
        f"${risk['weekly_loss_limit']:,.2f} "
        f"({risk['weekly_loss_pct']:.1f}% / {WEEKLY_LOSS_LIMIT_PCT*100:.0f}%)"
    )
    lines.append(
        f"  Stops Triggered:    {risk['stops_triggered']:>12}"
    )

    # --- Alerts ---
    if result["alerts"]:
        lines.append("")
        lines.append("--- Alerts ---")
        for alert in result["alerts"]:
            severity = alert["severity"]
            lines.append(f"  [{severity}] {alert['message']}")
    else:
        lines.append("")
        lines.append("--- Alerts ---")
        lines.append("  None. All risk checks passed.")

    lines.append("")
    lines.append("=" * 64)
    lines.append(
        f"  OVERALL STATUS: {status}"
    )
    lines.append("=" * 64)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Portfolio health check — automated Session Start workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Performs the complete CLAUDE.md Session Start sequence:
  1. Load portfolio from SQLite
  2. Fetch live prices from CLOB API for every open position
  3. Update current_price in DB
  4. Calculate per-position P&L and stop-loss status
  5. Check graduated drawdown thresholds (10/15/20%%)
  6. Check daily loss (5%%) and weekly loss (10%%) limits
  7. Output status: GREEN / YELLOW / RED

Examples:
  %(prog)s
  %(prog)s --portfolio my_portfolio
  %(prog)s --json
  %(prog)s --portfolio-db /path/to/portfolio.db --json
        """,
    )
    parser.add_argument(
        "--portfolio-db",
        type=str,
        default=str(DB_PATH),
        help=f"Path to portfolio SQLite database (default: {DB_PATH})",
    )
    parser.add_argument(
        "--portfolio",
        type=str,
        default=DEFAULT_PORTFOLIO_NAME,
        help="Portfolio name (default: 'default')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of human-readable format",
    )

    args = parser.parse_args()

    try:
        result = run_health_check(
            db_path=args.portfolio_db,
            portfolio_name=args.portfolio,
        )

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(format_human_readable(result))

        # Exit code reflects status: 0=GREEN, 1=YELLOW, 2=RED
        exit_codes = {"GREEN": 0, "YELLOW": 1, "RED": 2}
        sys.exit(exit_codes.get(result["status"], 1))

    except RuntimeError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
