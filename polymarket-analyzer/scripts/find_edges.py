#!/usr/bin/env python3
"""Scan active Polymarket markets for arbitrage edges.

Detects:
  - Underpriced markets: best-ask YES + best-ask NO < $1.00 (buy both for profit)
  - Overpriced markets: best-bid YES + best-bid NO > $1.00 (sell both for profit)
  - Wide spreads: markets where bid-ask spread creates opportunity

Uses Gamma API for market discovery and CLOB API for real order book prices.
Gamma mid-prices always sum to $1.00 by construction, so order book prices are
needed to find real executable edges.
"""

import argparse
import json
import sys
import time

import requests
from py_clob_client.client import ClobClient


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"


def fetch_markets(limit: int = 100, offset: int = 0) -> list[dict]:
    """Fetch active markets from Gamma API."""
    url = (
        f"{GAMMA_API}/markets"
        f"?limit={limit}&offset={offset}&active=true&closed=false"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_token_ids(market: dict) -> tuple[str, str] | None:
    """Extract YES and NO token IDs from a market dict."""
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    try:
        ids = json.loads(raw)
        if len(ids) < 2:
            return None
        return ids[0], ids[1]
    except (json.JSONDecodeError, ValueError, IndexError):
        return None


def parse_mid_prices(market: dict) -> tuple[float, float] | None:
    """Extract mid-prices from Gamma API (for display context)."""
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw)
        if len(prices) < 2:
            return None
        return float(prices[0]), float(prices[1])
    except (json.JSONDecodeError, ValueError, IndexError):
        return None


def calculate_fee(price: float, base_rate: float = 0.063) -> float:
    """Calculate dynamic taker fee rate for fee-bearing markets."""
    return base_rate * min(price, 1.0 - price)


def get_book_prices(client: ClobClient, token_id: str) -> tuple[float, float] | None:
    """Get best bid and best ask for a token. Returns (best_bid, best_ask) or None."""
    try:
        book = client.get_order_book(token_id)
    except Exception:
        return None

    bids = [(float(b.price), float(b.size)) for b in (book.bids or [])]
    asks = [(float(a.price), float(a.size)) for a in (book.asks or [])]

    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])

    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None

    if best_bid is None or best_ask is None:
        return None
    return best_bid, best_ask


def scan_edges(
    max_markets: int = 200,
    min_edge: float = 0.005,
    check_orderbooks: bool = True,
) -> list[dict]:
    """Scan markets for pricing edges.

    Two modes:
    1. Fast scan (check_orderbooks=False): Uses Gamma mid-prices (always sum to 1.0,
       so only finds spread-based opportunities via CLOB spot check)
    2. Deep scan (check_orderbooks=True): Fetches actual order book for each market
       to find real executable edges (slower, rate-limited)
    """
    client = ClobClient(CLOB_HOST) if check_orderbooks else None
    edges = []
    offset = 0
    batch_size = 100
    fetched = 0
    checked_books = 0

    while fetched < max_markets:
        batch = fetch_markets(limit=batch_size, offset=offset)
        if not batch:
            break

        for market in batch:
            token_ids = parse_token_ids(market)
            mid_prices = parse_mid_prices(market)

            if token_ids is None or mid_prices is None:
                continue

            yes_token_id, no_token_id = token_ids
            yes_mid, no_mid = mid_prices

            # Skip very low-liquidity markets
            liquidity = float(market.get("liquidityNum", 0) or 0)
            if liquidity < 100:
                continue

            if not check_orderbooks:
                continue

            # Fetch real order book prices
            yes_book = get_book_prices(client, yes_token_id)
            no_book = get_book_prices(client, no_token_id)
            checked_books += 1

            if yes_book is None or no_book is None:
                continue

            yes_bid, yes_ask = yes_book
            no_bid, no_ask = no_book

            # Check underpriced: buy YES at ask + buy NO at ask < $1.00
            buy_both_cost = yes_ask + no_ask
            if buy_both_cost < (1.0 - min_edge):
                raw_edge = 1.0 - buy_both_cost
                yes_fee = calculate_fee(yes_ask)
                no_fee = calculate_fee(no_ask)
                total_fee = yes_fee + no_fee
                net = raw_edge - total_fee

                edges.append({
                    "question": market.get("question", "Unknown"),
                    "slug": market.get("slug", ""),
                    "type": "UNDERPRICED",
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "cost_sum": round(buy_both_cost, 6),
                    "raw_edge": round(raw_edge, 6),
                    "fee_impact": round(total_fee, 6),
                    "net_profit_per_share": round(net, 6),
                    "profitable_after_fees": net > 0,
                    "yes_mid": yes_mid,
                    "no_mid": no_mid,
                    "volume_24h": market.get("volume24hr", 0) or 0,
                    "liquidity": liquidity,
                })

            # Check overpriced: sell YES at bid + sell NO at bid > $1.00
            sell_both_value = yes_bid + no_bid
            if sell_both_value > (1.0 + max(min_edge, 0.005)):
                raw_edge = sell_both_value - 1.0
                yes_fee = calculate_fee(yes_bid)
                no_fee = calculate_fee(no_bid)
                total_fee = yes_fee + no_fee
                net = raw_edge - total_fee

                edges.append({
                    "question": market.get("question", "Unknown"),
                    "slug": market.get("slug", ""),
                    "type": "OVERPRICED",
                    "yes_bid": yes_bid,
                    "no_bid": no_bid,
                    "cost_sum": round(sell_both_value, 6),
                    "raw_edge": round(raw_edge, 6),
                    "fee_impact": round(total_fee, 6),
                    "net_profit_per_share": round(net, 6),
                    "profitable_after_fees": net > 0,
                    "yes_mid": yes_mid,
                    "no_mid": no_mid,
                    "volume_24h": market.get("volume24hr", 0) or 0,
                    "liquidity": liquidity,
                })

            # Also report wide spreads (opportunity for market making)
            yes_spread = yes_ask - yes_bid
            no_spread = no_ask - no_bid
            max_spread = max(yes_spread, no_spread)
            if max_spread >= 0.03:  # 3 cent spread or wider
                edges.append({
                    "question": market.get("question", "Unknown"),
                    "slug": market.get("slug", ""),
                    "type": "WIDE_SPREAD",
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "yes_spread": round(yes_spread, 6),
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "no_spread": round(no_spread, 6),
                    "max_spread": round(max_spread, 6),
                    "raw_edge": round(max_spread, 6),
                    "fee_impact": 0.0,
                    "net_profit_per_share": round(max_spread, 6),
                    "profitable_after_fees": True,
                    "yes_mid": yes_mid,
                    "no_mid": no_mid,
                    "volume_24h": market.get("volume24hr", 0) or 0,
                    "liquidity": liquidity,
                })

            # Rate limit: avoid hammering the CLOB API
            if checked_books % 5 == 0:
                time.sleep(0.2)

        fetched += len(batch)
        offset += batch_size

        if len(batch) < batch_size:
            break

    # Sort by raw edge descending
    edges.sort(key=lambda x: x["raw_edge"], reverse=True)
    return edges


def format_output(edges: list[dict]) -> str:
    """Format edges for display."""
    if not edges:
        return (
            "No arbitrage edges found in current markets.\n"
            "This is normal -- Polymarket is well-arbitraged, with most\n"
            "opportunities lasting only ~2.7 seconds (median) in 2026."
        )

    lines = []

    # Group by type
    underpriced = [e for e in edges if e["type"] == "UNDERPRICED"]
    overpriced = [e for e in edges if e["type"] == "OVERPRICED"]
    wide_spread = [e for e in edges if e["type"] == "WIDE_SPREAD"]

    if underpriced:
        lines.append(f"\n=== UNDERPRICED ({len(underpriced)}) - Buy both sides for guaranteed profit ===\n")
        lines.append(f"  {'YES ask':>8} {'NO ask':>8} {'Sum':>8} {'Edge':>7} {'Net':>7} {'Vol24h':>10}  Question")
        lines.append("  " + "-" * 100)
        for e in underpriced:
            marker = " *" if e["profitable_after_fees"] else ""
            lines.append(
                f"  ${e['yes_ask']:<7.4f} ${e['no_ask']:<7.4f} "
                f"${e['cost_sum']:<7.4f} ${e['raw_edge']:<6.4f} "
                f"${e['net_profit_per_share']:<+6.4f}{marker} "
                f"${e['volume_24h']:>9,.0f}  {e['question'][:55]}"
            )

    if overpriced:
        lines.append(f"\n=== OVERPRICED ({len(overpriced)}) - Sell both sides ===\n")
        lines.append(f"  {'YES bid':>8} {'NO bid':>8} {'Sum':>8} {'Edge':>7} {'Net':>7} {'Vol24h':>10}  Question")
        lines.append("  " + "-" * 100)
        for e in overpriced:
            marker = " *" if e["profitable_after_fees"] else ""
            lines.append(
                f"  ${e['yes_bid']:<7.4f} ${e['no_bid']:<7.4f} "
                f"${e['cost_sum']:<7.4f} ${e['raw_edge']:<6.4f} "
                f"${e['net_profit_per_share']:<+6.4f}{marker} "
                f"${e['volume_24h']:>9,.0f}  {e['question'][:55]}"
            )

    if wide_spread:
        lines.append(f"\n=== WIDE SPREADS ({len(wide_spread)}) - Market-making opportunities ===\n")
        lines.append(f"  {'Y Spread':>8} {'N Spread':>8} {'Max':>7} {'Vol24h':>10} {'Liq':>10}  Question")
        lines.append("  " + "-" * 100)
        for e in wide_spread:
            lines.append(
                f"  ${e.get('yes_spread', 0):<7.4f} ${e.get('no_spread', 0):<7.4f} "
                f"${e.get('max_spread', 0):<6.4f} "
                f"${e['volume_24h']:>9,.0f}  "
                f"${e['liquidity']:>9,.0f}  "
                f"{e['question'][:55]}"
            )

    lines.append("")
    lines.append("* = profitable even on fee-bearing markets (most markets are fee-free)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Scan Polymarket for arbitrage edges using real order book data"
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.005,
        help="Minimum edge to report (default: 0.005 = $0.005/share)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum markets to scan (default: 200, each requires 2 API calls)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    print(f"Scanning up to {args.limit} markets (2 order book lookups each)...",
          file=sys.stderr)

    try:
        edges = scan_edges(
            max_markets=args.limit,
            min_edge=args.min_edge,
        )
    except requests.RequestException as e:
        print(f"Error fetching data: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(edges, indent=2))
    else:
        print(format_output(edges))


if __name__ == "__main__":
    main()
