#!/usr/bin/env python3
"""
Polymarket Paper Trading Engine

Simulates trades against live Polymarket data with zero financial risk.
Uses SQLite for persistent storage across agent sessions.
Fetches real prices from the CLOB and Gamma APIs.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_DIR = Path.home() / ".polymarket-paper"
DB_PATH = DB_DIR / "portfolio.db"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DEFAULT_BALANCE = 1000.0

# Risk defaults (overridable per-portfolio)
DEFAULT_RISK = {
    "max_position_pct": 0.10,       # 10% of bankroll per trade
    "max_drawdown_pct": 0.30,       # 30% total drawdown halts trading
    "max_concurrent_positions": 5,
    "daily_loss_limit_pct": 0.05,   # 5% of starting bankroll
    "max_single_market_pct": 0.20,  # 20% portfolio in one market
    "human_approval_pct": 0.15,     # trades > 15% need human approval
}

# Polymarket fee tiers — most markets are fee-free.
# Crypto 5-min / 15-min markets use a dynamic maker/taker model.
# We model the common case (0%) and let callers override.
DEFAULT_FEE_RATE = 0.0

# Token ID format: numeric string, typically 50-100 digits
import re
_TOKEN_ID_RE = re.compile(r"^\d{20,120}$")


def _validate_token_id(token_id: str) -> str:
    """Validate a CLOB token ID before using it in URLs."""
    if not isinstance(token_id, str) or not _TOKEN_ID_RE.match(token_id):
        raise ValueError(
            f"Invalid token ID format: must be 20-120 digits, got: {token_id!r}"
        )
    return token_id


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _api_get(url: str, timeout: int = 15) -> dict | list:
    """GET JSON from a URL. Returns parsed JSON."""
    req = Request(url, headers={"User-Agent": "polymarket-paper-trader/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (URLError, HTTPError) as exc:
        raise RuntimeError(f"API request failed: {url} — {exc}") from exc


def fetch_orderbook(token_id: str) -> dict:
    """Fetch the live order book for a CLOB token."""
    _validate_token_id(token_id)
    return _api_get(f"{CLOB_API}/book?token_id={token_id}")


def fetch_midpoint(token_id: str) -> float:
    """Fetch the midpoint price for a token."""
    _validate_token_id(token_id)
    data = _api_get(f"{CLOB_API}/midpoint?token_id={token_id}")
    return float(data["mid"])


def fetch_price(token_id: str, side: str) -> float:
    """Fetch the best price for a side (buy/sell)."""
    _validate_token_id(token_id)
    data = _api_get(f"{CLOB_API}/price?token_id={token_id}&side={side}")
    return float(data["price"])


def lookup_market(token_id: str) -> dict | None:
    """Look up market metadata by CLOB token ID via Gamma API."""
    _validate_token_id(token_id)
    data = _api_get(
        f"{GAMMA_API}/markets?clob_token_ids={token_id}&limit=1"
    )
    if data and len(data) > 0:
        return data[0]
    return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    """Open (and possibly initialize) the SQLite database."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS portfolios (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL DEFAULT 'default',
            starting_balance REAL NOT NULL,
            cash_balance  REAL NOT NULL,
            peak_value    REAL NOT NULL,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            risk_config   TEXT NOT NULL,
            active        INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
            token_id      TEXT NOT NULL,
            market_question TEXT,
            side          TEXT NOT NULL CHECK(side IN ('YES','NO')),
            shares        REAL NOT NULL DEFAULT 0,
            avg_entry     REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            opened_at     TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            closed        INTEGER NOT NULL DEFAULT 0,
            closed_at     TEXT,
            UNIQUE(portfolio_id, token_id, side, closed)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
            token_id      TEXT NOT NULL,
            market_question TEXT,
            side          TEXT NOT NULL CHECK(side IN ('YES','NO')),
            action        TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
            shares        REAL NOT NULL,
            price         REAL NOT NULL,
            fee           REAL NOT NULL DEFAULT 0,
            total_cost    REAL NOT NULL,
            reasoning     TEXT,
            executed_at   TEXT NOT NULL,
            entry_avg     REAL
        );

        CREATE TABLE IF NOT EXISTS daily_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
            date          TEXT NOT NULL,
            cash_balance  REAL NOT NULL,
            positions_value REAL NOT NULL,
            total_value   REAL NOT NULL,
            daily_pnl     REAL NOT NULL DEFAULT 0,
            UNIQUE(portfolio_id, date)
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Portfolio operations
# ---------------------------------------------------------------------------

def init_portfolio(
    starting_balance: float = DEFAULT_BALANCE,
    name: str = "default",
    risk_config: dict | None = None,
) -> dict:
    """Create a new paper-trading portfolio."""
    if starting_balance <= 0:
        raise ValueError("Starting balance must be positive")

    risk = {**DEFAULT_RISK, **(risk_config or {})}
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_db()
    try:
        # Deactivate existing portfolios with the same name
        conn.execute(
            "UPDATE portfolios SET active = 0 WHERE name = ? AND active = 1",
            (name,),
        )
        cur = conn.execute(
            """INSERT INTO portfolios
               (name, starting_balance, cash_balance, peak_value,
                created_at, updated_at, risk_config, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (name, starting_balance, starting_balance, starting_balance,
             now, now, json.dumps(risk)),
        )
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()

    return {
        "portfolio_id": pid,
        "name": name,
        "starting_balance": starting_balance,
        "cash_balance": starting_balance,
        "positions": [],
        "total_value": starting_balance,
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "created_at": now,
    }


def _active_portfolio(conn: sqlite3.Connection, name: str = "default") -> dict:
    """Fetch the active portfolio row or raise."""
    row = conn.execute(
        "SELECT * FROM portfolios WHERE name = ? AND active = 1 ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"No active portfolio '{name}'. Run: python paper_engine.py --action init"
        )
    return dict(row)


def get_portfolio(name: str = "default", refresh_prices: bool = True) -> dict:
    """Return the current portfolio state with live-priced positions."""
    conn = _get_db()
    try:
        pf = _active_portfolio(conn, name)
        pid = pf["id"]

        positions = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 0",
            (pid,),
        ).fetchall()

        pos_list = []
        positions_value = 0.0
        for p in positions:
            p = dict(p)
            if refresh_prices:
                try:
                    p["current_price"] = fetch_midpoint(p["token_id"])
                    conn.execute(
                        "UPDATE positions SET current_price = ?, updated_at = ? WHERE id = ?",
                        (p["current_price"],
                         datetime.now(timezone.utc).isoformat(), p["id"]),
                    )
                except Exception:
                    pass  # keep stale price
            value = p["shares"] * p["current_price"]
            unrealized_pnl = (p["current_price"] - p["avg_entry"]) * p["shares"]
            pos_list.append({
                "token_id": p["token_id"],
                "market_question": p["market_question"],
                "side": p["side"],
                "shares": p["shares"],
                "avg_entry": p["avg_entry"],
                "current_price": p["current_price"],
                "value": round(value, 4),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "opened_at": p["opened_at"],
            })
            positions_value += value

        total_value = pf["cash_balance"] + positions_value
        starting = pf["starting_balance"]
        pnl = total_value - starting

        # Update peak
        if total_value > pf["peak_value"]:
            conn.execute(
                "UPDATE portfolios SET peak_value = ?, updated_at = ? WHERE id = ?",
                (total_value, datetime.now(timezone.utc).isoformat(), pid),
            )

        conn.commit()

        return {
            "portfolio_id": pid,
            "name": pf["name"],
            "starting_balance": starting,
            "cash_balance": round(pf["cash_balance"], 4),
            "positions_value": round(positions_value, 4),
            "total_value": round(total_value, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / starting * 100, 2) if starting else 0,
            "peak_value": round(max(pf["peak_value"], total_value), 4),
            "drawdown_pct": round(
                (max(pf["peak_value"], total_value) - total_value)
                / max(pf["peak_value"], total_value) * 100, 2
            ) if max(pf["peak_value"], total_value) > 0 else 0,
            "positions": pos_list,
            "num_open_positions": len(pos_list),
            "created_at": pf["created_at"],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Order book fill simulation
# ---------------------------------------------------------------------------

def _simulate_fill(
    orderbook: dict,
    side: str,
    size_usd: float,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> dict:
    """
    Walk the order book to simulate a realistic fill.

    For a BUY: we consume asks (ascending price).
    For a SELL: we consume bids (descending price).

    Returns: {avg_price, shares_filled, total_cost, fee}
    """
    if side == "BUY":
        levels = orderbook.get("asks", [])
        # asks are already sorted ascending by CLOB
        levels = sorted(levels, key=lambda x: float(x["price"]))
    else:
        levels = orderbook.get("bids", [])
        levels = sorted(levels, key=lambda x: float(x["price"]), reverse=True)

    if not levels:
        raise RuntimeError(
            f"No {'asks' if side == 'BUY' else 'bids'} in order book — "
            "market may be illiquid or closed"
        )

    remaining_usd = size_usd
    total_shares = 0.0
    total_spent = 0.0

    for level in levels:
        price = float(level["price"])
        available_shares = float(level["size"])

        if price <= 0:
            continue

        # How many shares can we buy/sell at this level with remaining USD?
        max_shares_at_level = remaining_usd / price
        fill_shares = min(available_shares, max_shares_at_level)
        fill_cost = fill_shares * price

        total_shares += fill_shares
        total_spent += fill_cost
        remaining_usd -= fill_cost

        if remaining_usd < 0.001:  # close enough to zero
            break

    if total_shares == 0:
        raise RuntimeError("Could not fill any shares — check order size and book depth")

    avg_price = total_spent / total_shares
    fee = total_spent * fee_rate

    return {
        "avg_price": round(avg_price, 6),
        "shares_filled": round(total_shares, 4),
        "total_cost": round(total_spent + fee, 4),
        "fee": round(fee, 4),
        "levels_consumed": min(len(levels), 10),  # info only
    }


# ---------------------------------------------------------------------------
# Risk validation
# ---------------------------------------------------------------------------

def _validate_risk(
    portfolio: dict,
    risk_config: dict,
    side: str,
    size_usd: float,
    token_id: str,
) -> tuple[bool, str]:
    """Check trade against risk rules. Returns (ok, reason)."""
    total_value = portfolio["total_value"]
    starting = portfolio["starting_balance"]
    if total_value <= 0:
        return False, "Portfolio value is zero or negative"

    # Max position size
    max_pos = total_value * risk_config.get("max_position_pct", 0.10)
    if size_usd > max_pos:
        return False, (
            f"Trade size ${size_usd:.2f} exceeds max position "
            f"${max_pos:.2f} ({risk_config['max_position_pct']*100:.0f}% of portfolio)"
        )

    # Max drawdown
    peak = portfolio.get("peak_value", starting)
    if peak > 0:
        current_dd = (peak - total_value) / peak
        if current_dd >= risk_config.get("max_drawdown_pct", 0.30):
            return False, (
                f"Max drawdown exceeded: {current_dd*100:.1f}% "
                f"(limit {risk_config['max_drawdown_pct']*100:.0f}%)"
            )

    # Max concurrent positions (only for new positions)
    if side == "BUY":
        max_conc = risk_config.get("max_concurrent_positions", 5)
        if portfolio["num_open_positions"] >= max_conc:
            # Check if this is adding to an existing position
            existing = [p for p in portfolio["positions"]
                        if p["token_id"] == token_id]
            if not existing:
                return False, (
                    f"Max concurrent positions reached: "
                    f"{portfolio['num_open_positions']}/{max_conc}"
                )

    # Single market exposure
    existing_value = sum(
        p["value"] for p in portfolio["positions"]
        if p["token_id"] == token_id
    )
    new_exposure = existing_value + size_usd
    max_market = total_value * risk_config.get("max_single_market_pct", 0.20)
    if new_exposure > max_market:
        return False, (
            f"Single market exposure ${new_exposure:.2f} exceeds limit "
            f"${max_market:.2f} ({risk_config['max_single_market_pct']*100:.0f}%)"
        )

    # Human approval threshold
    approval_pct = risk_config.get("human_approval_pct", 0.15)
    if size_usd > total_value * approval_pct:
        return False, (
            f"Trade size ${size_usd:.2f} exceeds human approval threshold "
            f"({approval_pct*100:.0f}% of portfolio = ${total_value*approval_pct:.2f}). "
            f"Reduce size or set force=True to override."
        )

    return True, "OK"


def _check_daily_loss(
    conn: sqlite3.Connection,
    pid: int,
    starting_balance: float,
    risk_config: dict,
) -> tuple[bool, str]:
    """Check if daily loss limit has been exceeded."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Sum today's realized losses from SELL trades using the entry_avg
    # snapshot recorded at trade time (not the current positions table).
    row = conn.execute(
        """SELECT COALESCE(SUM(
            CASE WHEN action='SELL' AND entry_avg IS NOT NULL
                 THEN (price - entry_avg) * shares
                 ELSE 0 END
        ), 0) as daily_realized
        FROM trades
        WHERE portfolio_id = ? AND date(executed_at) = ?""",
        (pid, today),
    ).fetchone()

    daily_loss = abs(min(0, row["daily_realized"])) if row else 0
    limit = starting_balance * risk_config.get("daily_loss_limit_pct", 0.05)

    if daily_loss >= limit:
        return False, (
            f"Daily loss limit exceeded: ${daily_loss:.2f} "
            f"(limit ${limit:.2f} = {risk_config['daily_loss_limit_pct']*100:.0f}% "
            f"of starting balance)"
        )
    return True, "OK"


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def place_order(
    token_id: str,
    side: str,
    size: float,
    price: float | None = None,
    reasoning: str = "",
    portfolio_name: str = "default",
    fee_rate: float = DEFAULT_FEE_RATE,
    force: bool = False,
) -> dict:
    """
    Place a paper trade.

    Args:
        token_id: CLOB token ID
        side: 'YES' or 'NO'
        size: Amount in USD to spend
        price: Limit price (None = market order using live book)
        reasoning: Why this trade was made
        portfolio_name: Which portfolio to trade in
        fee_rate: Fee rate override (default 0 for most markets)
        force: Skip risk checks (except balance)

    Returns: Trade execution result dict.
    """
    side = side.upper()
    if side not in ("YES", "NO"):
        raise ValueError(f"Side must be YES or NO, got: {side}")
    if size <= 0:
        raise ValueError("Size must be positive")

    # Fetch market data and simulate fill BEFORE acquiring the write lock
    # so we don't hold the lock during network I/O.
    market_info = lookup_market(token_id)
    market_question = market_info["question"] if market_info else "Unknown market"

    if price is not None:
        # Limit order: fill at specified price
        shares = size / price
        fee = size * fee_rate
        fill = {
            "avg_price": price,
            "shares_filled": round(shares, 4),
            "total_cost": round(size + fee, 4),
            "fee": round(fee, 4),
        }
    else:
        # Market order: walk the real order book
        orderbook = fetch_orderbook(token_id)
        fill = _simulate_fill(orderbook, "BUY", size, fee_rate)

    # Get portfolio state for risk checks (also does network I/O)
    portfolio_state = get_portfolio(portfolio_name, refresh_prices=True)

    conn = _get_db()
    try:
        # Acquire exclusive write lock for atomic balance check + debit
        conn.execute("BEGIN IMMEDIATE")

        pf = _active_portfolio(conn, portfolio_name)
        pid = pf["id"]
        risk_config = json.loads(pf["risk_config"])

        # Balance check (always enforced) — re-read inside transaction
        if size > pf["cash_balance"]:
            conn.rollback()
            raise RuntimeError(
                f"Insufficient balance: need ${size:.2f}, "
                f"have ${pf['cash_balance']:.2f}"
            )

        # Risk validation
        if not force:
            ok, reason = _validate_risk(
                portfolio_state, risk_config, "BUY", size, token_id
            )
            if not ok:
                conn.rollback()
                raise RuntimeError(f"Risk check failed: {reason}")

            ok, reason = _check_daily_loss(
                conn, pid, pf["starting_balance"], risk_config
            )
            if not ok:
                conn.rollback()
                raise RuntimeError(f"Risk check failed: {reason}")

        now = datetime.now(timezone.utc).isoformat()

        # Update or create position
        existing = conn.execute(
            """SELECT * FROM positions
               WHERE portfolio_id = ? AND token_id = ? AND side = ? AND closed = 0""",
            (pid, token_id, side),
        ).fetchone()

        if existing:
            existing = dict(existing)
            old_shares = existing["shares"]
            old_avg = existing["avg_entry"]
            new_shares = old_shares + fill["shares_filled"]
            # Weighted average entry
            new_avg = (
                (old_avg * old_shares + fill["avg_price"] * fill["shares_filled"])
                / new_shares
            )
            conn.execute(
                """UPDATE positions
                   SET shares = ?, avg_entry = ?, current_price = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (round(new_shares, 4), round(new_avg, 6),
                 fill["avg_price"], now, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO positions
                   (portfolio_id, token_id, market_question, side, shares,
                    avg_entry, current_price, opened_at, updated_at, closed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (pid, token_id, market_question, side,
                 fill["shares_filled"], fill["avg_price"],
                 fill["avg_price"], now, now),
            )

        # Deduct from balance
        new_balance = pf["cash_balance"] - fill["total_cost"]
        conn.execute(
            "UPDATE portfolios SET cash_balance = ?, updated_at = ? WHERE id = ?",
            (round(new_balance, 4), now, pid),
        )

        # Compute avg entry at trade time for accurate daily loss tracking
        if existing:
            existing = dict(existing) if not isinstance(existing, dict) else existing
            trade_entry_avg = existing["avg_entry"]
        else:
            trade_entry_avg = fill["avg_price"]

        # Record trade (includes entry_avg snapshot for daily loss calculation)
        conn.execute(
            """INSERT INTO trades
               (portfolio_id, token_id, market_question, side, action,
                shares, price, fee, total_cost, reasoning, executed_at,
                entry_avg)
               VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?)""",
            (pid, token_id, market_question, side,
             fill["shares_filled"], fill["avg_price"], fill["fee"],
             fill["total_cost"], reasoning, now, fill["avg_price"]),
        )

        conn.commit()

        return {
            "status": "filled",
            "action": "BUY",
            "side": side,
            "token_id": token_id,
            "market": market_question,
            "shares": fill["shares_filled"],
            "avg_price": fill["avg_price"],
            "fee": fill["fee"],
            "total_cost": fill["total_cost"],
            "new_balance": round(new_balance, 4),
            "reasoning": reasoning,
            "executed_at": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_position(
    token_id: str,
    side: str | None = None,
    portfolio_name: str = "default",
    fee_rate: float = DEFAULT_FEE_RATE,
    reasoning: str = "",
) -> dict:
    """
    Close an open position at current market price.

    Args:
        token_id: The CLOB token to close
        side: YES or NO (auto-detected if only one position exists)
        portfolio_name: Which portfolio
        fee_rate: Override fee rate
        reasoning: Why closing

    Returns: Close execution result.
    """
    # Fetch order book BEFORE acquiring write lock (network I/O)
    orderbook = fetch_orderbook(token_id)

    # Walk bids to simulate sell fill
    bids = sorted(
        orderbook.get("bids", []),
        key=lambda x: float(x["price"]),
        reverse=True,
    )
    if not bids:
        raise RuntimeError("No bids in order book — cannot close position")

    conn = _get_db()
    try:
        # Acquire exclusive write lock for atomic credit
        conn.execute("BEGIN IMMEDIATE")

        pf = _active_portfolio(conn, portfolio_name)
        pid = pf["id"]

        if side:
            side = side.upper()
            positions = conn.execute(
                """SELECT * FROM positions
                   WHERE portfolio_id = ? AND token_id = ? AND side = ? AND closed = 0""",
                (pid, token_id, side),
            ).fetchall()
        else:
            positions = conn.execute(
                """SELECT * FROM positions
                   WHERE portfolio_id = ? AND token_id = ? AND closed = 0""",
                (pid, token_id),
            ).fetchall()

        if not positions:
            conn.rollback()
            raise RuntimeError(
                f"No open position for token {token_id}"
                + (f" side={side}" if side else "")
            )

        results = []
        for pos in positions:
            pos = dict(pos)

            remaining_shares = pos["shares"]
            total_proceeds = 0.0
            for level in bids:
                lvl_price = float(level["price"])
                lvl_size = float(level["size"])
                sell_shares = min(remaining_shares, lvl_size)
                total_proceeds += sell_shares * lvl_price
                remaining_shares -= sell_shares
                if remaining_shares < 0.0001:
                    break

            shares_sold = pos["shares"] - remaining_shares
            if shares_sold <= 0:
                conn.rollback()
                raise RuntimeError("Could not sell any shares at current bids")

            avg_sell_price = total_proceeds / shares_sold if shares_sold > 0 else 0
            fee = total_proceeds * fee_rate
            net_proceeds = total_proceeds - fee

            pnl = (avg_sell_price - pos["avg_entry"]) * shares_sold - fee

            now = datetime.now(timezone.utc).isoformat()

            # Mark position closed
            conn.execute(
                "UPDATE positions SET closed = 1, closed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, pos["id"]),
            )

            # Credit proceeds to balance
            new_balance = pf["cash_balance"] + net_proceeds
            conn.execute(
                "UPDATE portfolios SET cash_balance = ?, updated_at = ? WHERE id = ?",
                (round(new_balance, 4), now, pid),
            )
            pf["cash_balance"] = new_balance

            # Record trade with entry_avg snapshot for daily loss tracking
            conn.execute(
                """INSERT INTO trades
                   (portfolio_id, token_id, market_question, side, action,
                    shares, price, fee, total_cost, reasoning, executed_at,
                    entry_avg)
                   VALUES (?, ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?)""",
                (pid, token_id, pos["market_question"], pos["side"],
                 round(shares_sold, 4), round(avg_sell_price, 6),
                 round(fee, 4), round(net_proceeds, 4), reasoning, now,
                 pos["avg_entry"]),
            )

            results.append({
                "status": "closed",
                "action": "SELL",
                "side": pos["side"],
                "token_id": token_id,
                "market": pos["market_question"],
                "shares_sold": round(shares_sold, 4),
                "avg_sell_price": round(avg_sell_price, 6),
                "avg_entry_price": pos["avg_entry"],
                "fee": round(fee, 4),
                "net_proceeds": round(net_proceeds, 4),
                "realized_pnl": round(pnl, 4),
                "new_balance": round(new_balance, 4),
                "executed_at": now,
            })

        conn.commit()
        return results[0] if len(results) == 1 else results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_trades(
    portfolio_name: str = "default",
    limit: int = 50,
) -> list[dict]:
    """Return trade history, most recent first."""
    conn = _get_db()
    try:
        pf = _active_portfolio(conn, portfolio_name)
        rows = conn.execute(
            """SELECT * FROM trades
               WHERE portfolio_id = ?
               ORDER BY executed_at DESC
               LIMIT ?""",
            (pf["id"], limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daily snapshot
# ---------------------------------------------------------------------------

def take_snapshot(portfolio_name: str = "default") -> dict:
    """Record a daily portfolio snapshot for performance tracking."""
    state = get_portfolio(portfolio_name, refresh_prices=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    conn = _get_db()
    try:
        pid = state["portfolio_id"]

        # Get yesterday's snapshot for daily P&L
        prev = conn.execute(
            """SELECT total_value FROM daily_snapshots
               WHERE portfolio_id = ? AND date < ?
               ORDER BY date DESC LIMIT 1""",
            (pid, today),
        ).fetchone()

        prev_value = prev["total_value"] if prev else state["starting_balance"]
        daily_pnl = state["total_value"] - prev_value

        conn.execute(
            """INSERT OR REPLACE INTO daily_snapshots
               (portfolio_id, date, cash_balance, positions_value,
                total_value, daily_pnl)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (pid, today, state["cash_balance"], state["positions_value"],
             state["total_value"], round(daily_pnl, 4)),
        )
        conn.commit()

        return {
            "date": today,
            "total_value": state["total_value"],
            "daily_pnl": round(daily_pnl, 4),
            "cash": state["cash_balance"],
            "positions_value": state["positions_value"],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_portfolio(pf: dict) -> str:
    """Format portfolio state for human-readable output."""
    lines = [
        f"=== Portfolio: {pf['name']} ===",
        f"Starting Balance:  ${pf['starting_balance']:>10,.2f}",
        f"Cash Balance:      ${pf['cash_balance']:>10,.2f}",
        f"Positions Value:   ${pf['positions_value']:>10,.2f}",
        f"Total Value:       ${pf['total_value']:>10,.2f}",
        f"P&L:               ${pf['pnl']:>10,.2f} ({pf['pnl_pct']:+.2f}%)",
        f"Peak Value:        ${pf['peak_value']:>10,.2f}",
        f"Drawdown:          {pf['drawdown_pct']:>10.2f}%",
        f"Open Positions:    {pf['num_open_positions']:>10d}",
        f"Created:           {pf['created_at']}",
    ]
    if pf["positions"]:
        lines.append("\n--- Open Positions ---")
        for p in pf["positions"]:
            pnl_str = f"${p['unrealized_pnl']:+,.2f}"
            lines.append(
                f"  {p['side']:>3} {p['shares']:>8.2f} shares @ "
                f"${p['avg_entry']:.4f} -> ${p['current_price']:.4f}  "
                f"P&L: {pnl_str}"
            )
            if p["market_question"]:
                lines.append(f"      {p['market_question'][:70]}")
    return "\n".join(lines)


def _format_trades(trades: list[dict]) -> str:
    """Format trade list for human-readable output."""
    if not trades:
        return "No trades recorded."
    lines = ["=== Trade History ==="]
    for t in trades:
        lines.append(
            f"  [{t['executed_at'][:19]}] {t['action']:>4} {t['side']:>3} "
            f"{t['shares']:>8.2f} @ ${t['price']:.4f} "
            f"(cost: ${t['total_cost']:.2f}, fee: ${t['fee']:.2f})"
        )
        if t.get("market_question"):
            lines.append(f"    {t['market_question'][:70]}")
        if t.get("reasoning"):
            lines.append(f"    Reason: {t['reasoning'][:70]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Paper Trading Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --action init --balance 1000
  %(prog)s --action buy --token TOKEN_ID --side YES --size 50
  %(prog)s --action sell --token TOKEN_ID --side YES --size 50
  %(prog)s --action close --token TOKEN_ID
  %(prog)s --action portfolio
  %(prog)s --action trades
  %(prog)s --action snapshot
        """,
    )
    parser.add_argument("--action", required=True,
                        choices=["init", "buy", "sell", "close",
                                 "portfolio", "trades", "snapshot"],
                        help="Action to perform")
    parser.add_argument("--balance", type=float, default=DEFAULT_BALANCE,
                        help="Starting balance (init only)")
    parser.add_argument("--name", default="default",
                        help="Portfolio name")
    parser.add_argument("--token", help="CLOB token ID")
    parser.add_argument("--side", choices=["YES", "NO", "yes", "no"],
                        help="Trade side")
    parser.add_argument("--size", type=float, help="Trade size in USD")
    parser.add_argument("--price", type=float, default=None,
                        help="Limit price (omit for market order)")
    parser.add_argument("--reason", default="", help="Trade reasoning")
    parser.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE,
                        help="Fee rate override")
    parser.add_argument("--force", action="store_true",
                        help="Skip risk checks")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max trades to show")

    args = parser.parse_args()

    try:
        if args.action == "init":
            result = init_portfolio(args.balance, args.name)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"Portfolio '{result['name']}' initialized with "
                      f"${result['starting_balance']:,.2f}")

        elif args.action in ("buy", "sell"):
            if not args.token:
                parser.error("--token is required for buy/sell")
            if not args.side:
                parser.error("--side is required for buy/sell")
            if not args.size:
                parser.error("--size is required for buy/sell")

            result = place_order(
                token_id=args.token,
                side=args.side.upper(),
                size=args.size,
                price=args.price,
                reasoning=args.reason,
                portfolio_name=args.name,
                fee_rate=args.fee_rate,
                force=args.force,
            )
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(
                    f"{result['action']} {result['side']} "
                    f"{result['shares']:.2f} shares @ "
                    f"${result['avg_price']:.4f}\n"
                    f"Market: {result['market']}\n"
                    f"Total cost: ${result['total_cost']:.2f} "
                    f"(fee: ${result['fee']:.2f})\n"
                    f"New balance: ${result['new_balance']:.2f}"
                )

        elif args.action == "close":
            if not args.token:
                parser.error("--token is required for close")
            result = close_position(
                token_id=args.token,
                side=args.side.upper() if args.side else None,
                portfolio_name=args.name,
                fee_rate=args.fee_rate,
                reasoning=args.reason,
            )
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                if isinstance(result, list):
                    for r in result:
                        print(
                            f"Closed {r['side']} position: "
                            f"{r['shares_sold']:.2f} shares @ "
                            f"${r['avg_sell_price']:.4f}\n"
                            f"Realized P&L: ${r['realized_pnl']:+,.2f}\n"
                            f"New balance: ${r['new_balance']:.2f}"
                        )
                else:
                    print(
                        f"Closed {result['side']} position: "
                        f"{result['shares_sold']:.2f} shares @ "
                        f"${result['avg_sell_price']:.4f}\n"
                        f"Realized P&L: ${result['realized_pnl']:+,.2f}\n"
                        f"New balance: ${result['new_balance']:.2f}"
                    )

        elif args.action == "portfolio":
            result = get_portfolio(args.name, refresh_prices=True)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(_format_portfolio(result))

        elif args.action == "trades":
            result = get_trades(args.name, args.limit)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
            else:
                print(_format_trades(result))

        elif args.action == "snapshot":
            result = take_snapshot(args.name)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(
                    f"Snapshot for {result['date']}: "
                    f"${result['total_value']:,.2f} "
                    f"(daily P&L: ${result['daily_pnl']:+,.2f})"
                )

    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
