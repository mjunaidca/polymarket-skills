#!/usr/bin/env python3
"""Generate ranked trade recommendations for Polymarket prediction markets.

Scans active markets, scores edges (arbitrage, momentum, orderbook imbalance),
applies Kelly criterion sizing, validates against risk rules, and outputs
actionable trade recommendations as JSON.

Usage:
    python advisor.py --top 5
    python advisor.py --portfolio-db ~/.polymarket-paper/portfolio.db --top 5
    python advisor.py --min-volume 50000 --min-edge 0.03 --top 10
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

DEFAULT_PORTFOLIO_VALUE = 10000.0
DEFAULT_MAX_POSITION_PCT = 0.10
DEFAULT_MAX_OPEN_POSITIONS = 5
DEFAULT_MIN_EDGE = 0.03
DEFAULT_MIN_VOLUME = 10000.0
DEFAULT_MIN_CONFIDENCE = 0.5


def fetch_markets(limit=100, min_volume=0):
    """Fetch active markets from Gamma API sorted by 24h volume."""
    params = {
        "limit": min(limit, 100),
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=30)
    resp.raise_for_status()
    raw = resp.json()

    markets = []
    for m in raw:
        vol_24h = float(m.get("volume24hr", 0) or 0)
        if vol_24h < min_volume:
            continue
        if not m.get("acceptingOrders", False):
            continue

        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
        except (json.JSONDecodeError, TypeError):
            outcomes = []
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            prices = [float(p) for p in prices]
        except (json.JSONDecodeError, TypeError, ValueError):
            prices = []
        try:
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
        except (json.JSONDecodeError, TypeError):
            token_ids = []

        # Only handle binary markets (2 outcomes) for now
        if len(outcomes) != 2 or len(prices) != 2 or len(token_ids) != 2:
            continue

        end_date = m.get("endDate", "")
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < 24:
                    continue
            except (ValueError, TypeError):
                pass

        markets.append({
            "question": m.get("question", ""),
            "slug": m.get("slug", ""),
            "condition_id": m.get("conditionID", ""),
            "outcomes": outcomes,
            "prices": prices,
            "token_ids": token_ids,
            "volume_24h": vol_24h,
            "liquidity": float(m.get("liquidityNum", 0) or 0),
            "end_date": end_date,
        })

    return markets


def fetch_orderbook(token_id):
    """Fetch orderbook for a token from CLOB API."""
    try:
        resp = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def calculate_spread(orderbook):
    """Calculate spread and imbalance from orderbook data."""
    if not orderbook:
        return None

    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks:
        return None

    best_bid = float(bids[0].get("price", 0))
    best_ask = float(asks[0].get("price", 1))
    spread = best_ask - best_bid
    midpoint = (best_bid + best_ask) / 2

    bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
    ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
    total_depth = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": spread / midpoint if midpoint > 0 else 0,
        "midpoint": midpoint,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "imbalance": imbalance,
    }


def detect_arbitrage(yes_price, no_price):
    """Detect YES+NO arbitrage. Returns edge if underpriced."""
    total = yes_price + no_price
    if total < 0.99:  # Underpriced: buying both sides guarantees profit
        return {
            "type": "arbitrage",
            "edge": 1.0 - total,
            "direction": "both",
            "detail": f"YES+NO={total:.4f}, guaranteed ${1.0 - total:.4f}/share profit",
        }
    return None


def detect_momentum(imbalance, volume_24h, liquidity):
    """Detect momentum signal from orderbook imbalance and volume."""
    if liquidity <= 0:
        return None

    volume_liquidity_ratio = volume_24h / liquidity
    # High volume relative to liquidity + orderbook imbalance = momentum
    if abs(imbalance) > 0.3 and volume_liquidity_ratio > 2.0:
        direction = "YES" if imbalance > 0 else "NO"
        strength = min(abs(imbalance) * volume_liquidity_ratio / 10, 1.0)
        edge = abs(imbalance) * 0.15  # Conservative edge estimate
        return {
            "type": "momentum",
            "edge": edge,
            "direction": direction,
            "detail": (
                f"Orderbook imbalance={imbalance:+.2f}, "
                f"volume/liquidity={volume_liquidity_ratio:.1f}x, "
                f"momentum favors {direction}"
            ),
            "strength": strength,
        }
    return None


def detect_spread_opportunity(spread_pct, midpoint):
    """Detect wide-spread mean reversion opportunity."""
    # If spread is wide (5-10%), there may be a mean reversion opportunity
    # by placing a limit order at the midpoint
    if 0.05 <= spread_pct <= 0.10 and 0.15 < midpoint < 0.85:
        edge = spread_pct * 0.3  # Conservatively capture 30% of spread
        return {
            "type": "mean-reversion",
            "edge": edge,
            "direction": "YES" if midpoint < 0.5 else "NO",
            "detail": (
                f"Wide spread={spread_pct:.1%}, midpoint={midpoint:.3f}. "
                f"Limit order near midpoint captures spread."
            ),
        }
    return None


def kelly_half(estimated_prob, market_price, side="YES"):
    """Calculate half-Kelly position fraction for a binary market.

    Args:
        estimated_prob: Your estimated probability that YES resolves to 1.
        market_price: Current price of the side you are buying.
        side: "YES" or "NO".

    Returns:
        Half-Kelly fraction (0 to 1), or 0 if negative EV.
    """
    if side == "YES":
        p = estimated_prob
        cost = market_price
    else:
        p = 1.0 - estimated_prob
        cost = market_price

    if cost <= 0 or cost >= 1:
        return 0

    # Payout is 1.0 per share, cost is market_price
    # b = net payout / cost = (1 - cost) / cost
    b = (1.0 - cost) / cost
    q = 1.0 - p
    if b <= 0:
        return 0

    kelly = (b * p - q) / b
    return max(0, kelly * 0.5)


def load_portfolio(db_path):
    """Load portfolio state from paper trader SQLite database.

    Returns dict with keys: value, cash, positions, peak_value, daily_pnl,
    open_position_count. Returns defaults if DB does not exist.
    """
    if not db_path or not os.path.exists(db_path):
        return {
            "value": DEFAULT_PORTFOLIO_VALUE,
            "cash": DEFAULT_PORTFOLIO_VALUE,
            "positions": [],
            "peak_value": DEFAULT_PORTFOLIO_VALUE,
            "daily_pnl": 0.0,
            "open_position_count": 0,
        }

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Try to read portfolio summary
        portfolio = {
            "value": DEFAULT_PORTFOLIO_VALUE,
            "cash": DEFAULT_PORTFOLIO_VALUE,
            "positions": [],
            "peak_value": DEFAULT_PORTFOLIO_VALUE,
            "daily_pnl": 0.0,
            "open_position_count": 0,
        }

        # Read account balance from portfolios table
        try:
            cur.execute(
                "SELECT cash_balance, peak_value FROM portfolios "
                "WHERE active = 1 ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                portfolio["cash"] = float(row["cash_balance"])
                portfolio["peak_value"] = float(row["peak_value"])
                # Calculate total value: cash + positions value
                pos_cur = conn.cursor()
                pos_cur.execute(
                    "SELECT COALESCE(SUM(shares * current_price), 0) as pos_val "
                    "FROM positions WHERE portfolio_id = 1 AND closed = 0"
                )
                pos_row = pos_cur.fetchone()
                pos_val = float(pos_row["pos_val"]) if pos_row else 0.0
                portfolio["value"] = portfolio["cash"] + pos_val
        except sqlite3.OperationalError:
            pass

        # Read open positions
        try:
            cur.execute(
                "SELECT token_id, side, shares, avg_entry, market_question "
                "FROM positions WHERE closed = 0"
            )
            positions = [dict(r) for r in cur.fetchall()]
            portfolio["positions"] = positions
            portfolio["open_position_count"] = len(positions)
        except sqlite3.OperationalError:
            pass

        # Read daily P&L from daily_snapshots
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            cur.execute(
                "SELECT daily_pnl FROM daily_snapshots "
                "WHERE date = ? ORDER BY id DESC LIMIT 1",
                (today,),
            )
            row = cur.fetchone()
            if row:
                portfolio["daily_pnl"] = float(row["daily_pnl"])
        except sqlite3.OperationalError:
            pass

        conn.close()
        return portfolio

    except sqlite3.Error:
        return {
            "value": DEFAULT_PORTFOLIO_VALUE,
            "cash": DEFAULT_PORTFOLIO_VALUE,
            "positions": [],
            "peak_value": DEFAULT_PORTFOLIO_VALUE,
            "daily_pnl": 0.0,
            "open_position_count": 0,
        }


def check_risk_rules(portfolio, position_size_usdc, confidence):
    """Validate a proposed trade against risk rules.

    Returns (passed: bool, reason: str).
    """
    pv = portfolio["value"]
    if pv <= 0:
        return False, "Portfolio value is zero or negative"

    # Daily loss limit: 5%
    if portfolio["daily_pnl"] < -pv * 0.05:
        return False, f"Daily loss limit exceeded: {portfolio['daily_pnl']:.2f}"

    # Drawdown limit: 20%
    if portfolio["peak_value"] > 0:
        drawdown = (portfolio["peak_value"] - pv) / portfolio["peak_value"]
        if drawdown > 0.20:
            return False, f"Max drawdown exceeded: {drawdown:.1%}"

    # Max open positions: 5
    if portfolio["open_position_count"] >= DEFAULT_MAX_OPEN_POSITIONS:
        return False, f"Max open positions reached: {portfolio['open_position_count']}"

    # Position size cap
    max_pct = DEFAULT_MAX_POSITION_PCT
    if confidence < 0.7:
        max_pct = 0.05
    max_size = pv * max_pct
    if position_size_usdc > max_size:
        return False, (
            f"Position too large: ${position_size_usdc:.2f} > "
            f"${max_size:.2f} ({max_pct:.0%} of portfolio)"
        )

    return True, "OK"


def score_market(market, portfolio):
    """Analyze a single market and return a trade recommendation or None."""
    yes_price = market["prices"][0]
    no_price = market["prices"][1]

    # Skip markets priced at extremes (already resolved in practice)
    if yes_price < 0.03 or yes_price > 0.97:
        return None

    # Fetch orderbook for the YES token (used for imbalance/depth signals)
    ob = fetch_orderbook(market["token_ids"][0])
    spread_info = calculate_spread(ob)

    # Detect edges, pick the strongest
    edges = []

    arb = detect_arbitrage(yes_price, no_price)
    if arb:
        edges.append(arb)

    if spread_info:
        mom = detect_momentum(
            spread_info["imbalance"],
            market["volume_24h"],
            market["liquidity"],
        )
        if mom:
            edges.append(mom)

        # Mean reversion: only valid when orderbook spread is reasonable
        # (under 20%), otherwise the midpoint is meaningless
        if spread_info["spread_pct"] < 0.20:
            gamma_spread = abs(yes_price - spread_info["midpoint"])
            if gamma_spread > 0.02 and 0.15 < yes_price < 0.85:
                edges.append({
                    "type": "mean-reversion",
                    "edge": gamma_spread * 0.5,
                    "direction": "YES" if yes_price < spread_info["midpoint"] else "NO",
                    "detail": (
                        f"Gamma price {yes_price:.3f} deviates from orderbook "
                        f"midpoint {spread_info['midpoint']:.3f} by "
                        f"{gamma_spread:.3f} (book spread {spread_info['spread_pct']:.1%})"
                    ),
                })

    if not edges:
        return None

    # Pick the edge with highest expected value
    best = max(edges, key=lambda e: e["edge"])

    if best["edge"] < DEFAULT_MIN_EDGE:
        return None

    # Determine trade side and entry price
    if best["type"] == "arbitrage":
        side = "YES"  # Will also need NO side, noted in reasoning
        entry_price = yes_price
        estimated_prob = 0.5  # Irrelevant for arb, size differently
    elif best["direction"] == "YES":
        side = "YES"
        entry_price = yes_price
        estimated_prob = min(yes_price + best["edge"], 0.95)
    else:
        side = "NO"
        entry_price = no_price
        estimated_prob = min(no_price + best["edge"], 0.95)

    # Calculate confidence (0-1)
    if best["type"] == "arbitrage":
        confidence = min(best["edge"] / 0.05, 1.0)  # 5% edge = max confidence
    elif best["type"] == "momentum":
        confidence = best.get("strength", 0.5)
    else:
        confidence = min(best["edge"] / 0.10, 0.9)

    confidence = max(DEFAULT_MIN_CONFIDENCE, min(confidence, 1.0))

    # Position sizing via half-Kelly
    if best["type"] == "arbitrage":
        # For arb, size is based on guaranteed return
        kelly_frac = min(best["edge"] * 2, DEFAULT_MAX_POSITION_PCT)
    else:
        kelly_frac = kelly_half(estimated_prob, entry_price, side)

    position_size_usdc = portfolio["value"] * kelly_frac

    # Apply hard caps
    max_pct = DEFAULT_MAX_POSITION_PCT
    if best["type"] == "arbitrage":
        max_pct = 0.20  # Higher cap for hedged arb
    elif confidence < 0.7:
        max_pct = 0.05
    elif best["type"] == "momentum":
        max_pct = 0.05  # News-like, capped lower
    position_size_usdc = min(position_size_usdc, portfolio["value"] * max_pct)

    # Minimum trade size
    if position_size_usdc < 10:
        return None

    # Risk check
    passed, reason = check_risk_rules(portfolio, position_size_usdc, confidence)
    if not passed:
        return {
            "market": market["question"],
            "skipped": True,
            "skip_reason": reason,
        }

    # Stop loss and target
    if best["type"] == "arbitrage":
        target = 1.0
        stop_loss = None  # Arb is held to resolution
    else:
        target = entry_price + best["edge"] * 0.8
        stop_loss = entry_price - best["edge"] * 0.5
        target = round(min(target, 0.99), 4)
        stop_loss = round(max(stop_loss, 0.01), 4)

    ev = best["edge"] * position_size_usdc
    risk_amount = (entry_price - (stop_loss or 0)) * (position_size_usdc / entry_price) if stop_loss else 0
    reward_amount = (target - entry_price) * (position_size_usdc / entry_price)
    risk_reward = reward_amount / risk_amount if risk_amount > 0 else float("inf")

    return {
        "market": market["question"],
        "url": f"https://polymarket.com/event/{market['slug']}",
        "side": side,
        "token_id": market["token_ids"][0 if side == "YES" else 1],
        "entry_price": round(entry_price, 4),
        "size_usdc": round(position_size_usdc, 2),
        "shares": round(position_size_usdc / entry_price, 2) if entry_price > 0 else 0,
        "confidence": round(confidence, 3),
        "edge_type": best["type"],
        "edge": round(best["edge"], 4),
        "reasoning": best["detail"],
        "target": target,
        "stop_loss": stop_loss,
        "expected_value": round(ev, 2),
        "risk_reward": round(risk_reward, 2) if risk_reward != float("inf") else "inf",
        "skipped": False,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate ranked trade recommendations for Polymarket"
    )
    parser.add_argument(
        "--portfolio-db", type=str, default=None,
        help="Path to paper trader SQLite database (default: use $10K virtual portfolio)"
    )
    parser.add_argument(
        "--top", type=int, default=5,
        help="Number of top recommendations to output (default: 5)"
    )
    parser.add_argument(
        "--min-volume", type=float, default=DEFAULT_MIN_VOLUME,
        help=f"Minimum 24h volume filter (default: {DEFAULT_MIN_VOLUME})"
    )
    parser.add_argument(
        "--min-edge", type=float, default=DEFAULT_MIN_EDGE,
        help=f"Minimum edge threshold (default: {DEFAULT_MIN_EDGE})"
    )
    parser.add_argument(
        "--scan-limit", type=int, default=100,
        help="Number of markets to scan from Gamma API (default: 100)"
    )

    args = parser.parse_args()

    # Load portfolio state
    portfolio = load_portfolio(args.portfolio_db)

    # Fetch and filter markets
    try:
        markets = fetch_markets(limit=args.scan_limit, min_volume=args.min_volume)
    except requests.RequestException as e:
        print(json.dumps({"error": f"Failed to fetch markets: {e}"}), file=sys.stderr)
        sys.exit(1)

    if not markets:
        print(json.dumps({
            "recommendations": [],
            "summary": "No markets passed filters",
            "markets_scanned": 0,
        }, indent=2))
        return

    # Score each market
    recommendations = []
    skipped = []
    for market in markets:
        result = score_market(market, portfolio)
        if result is None:
            continue
        if result.get("skipped"):
            skipped.append(result)
        else:
            recommendations.append(result)

    # Sort by expected value descending
    recommendations.sort(key=lambda r: r["expected_value"], reverse=True)

    # Take top N
    top_recs = recommendations[:args.top]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "portfolio": {
            "value": portfolio["value"],
            "cash": portfolio["cash"],
            "open_positions": portfolio["open_position_count"],
            "daily_pnl": portfolio["daily_pnl"],
        },
        "markets_scanned": len(markets),
        "opportunities_found": len(recommendations),
        "skipped_risk": len(skipped),
        "recommendations": top_recs,
    }

    if skipped:
        output["skipped_trades"] = skipped[:5]

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
