#!/usr/bin/env python3
"""Analyze a Polymarket order book for a given token ID.

Calculates spread, depth, bid-ask imbalance, and classifies book thickness.

Requires: py-clob-client (pip install py-clob-client)
"""

import argparse
import json
import sys

from py_clob_client.client import ClobClient


CLOB_HOST = "https://clob.polymarket.com"


def fetch_orderbook(token_id: str) -> object:
    """Fetch order book from CLOB API."""
    client = ClobClient(CLOB_HOST)
    return client.get_order_book(token_id)


def analyze(book, depth: int = 5) -> dict:
    """Analyze an order book and return metrics."""
    bids = [(float(b.price), float(b.size)) for b in (book.bids or [])]
    asks = [(float(a.price), float(a.size)) for a in (book.asks or [])]

    # Sort: bids descending by price, asks ascending by price
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])

    result = {
        "token_id": book.asset_id,
        "total_bid_levels": len(bids),
        "total_ask_levels": len(asks),
    }

    if not bids and not asks:
        result["status"] = "EMPTY_BOOK"
        return result

    # Best bid / best ask
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 1.0
    spread = best_ask - best_bid
    mid_price = (best_bid + best_ask) / 2.0 if (bids and asks) else None

    result["best_bid"] = best_bid
    result["best_ask"] = best_ask
    result["spread"] = round(spread, 6)
    result["spread_pct"] = round((spread / mid_price * 100) if mid_price else 0, 4)
    result["mid_price"] = round(mid_price, 6) if mid_price else None

    # Depth at top N levels
    top_bids = bids[:depth]
    top_asks = asks[:depth]

    bid_depth = sum(size for _, size in top_bids)
    ask_depth = sum(size for _, size in top_asks)
    total_depth = bid_depth + ask_depth

    result["bid_depth"] = round(bid_depth, 2)
    result["ask_depth"] = round(ask_depth, 2)
    result["total_depth"] = round(total_depth, 2)
    result["depth_levels_used"] = depth

    # Bid-ask imbalance ratio: positive = more bids (buying pressure)
    if total_depth > 0:
        imbalance = (bid_depth - ask_depth) / total_depth
    else:
        imbalance = 0.0
    result["imbalance_ratio"] = round(imbalance, 4)

    # Classify the imbalance
    if imbalance > 0.3:
        result["imbalance_signal"] = "STRONG_BUY_PRESSURE"
    elif imbalance > 0.1:
        result["imbalance_signal"] = "MODERATE_BUY_PRESSURE"
    elif imbalance < -0.3:
        result["imbalance_signal"] = "STRONG_SELL_PRESSURE"
    elif imbalance < -0.1:
        result["imbalance_signal"] = "MODERATE_SELL_PRESSURE"
    else:
        result["imbalance_signal"] = "BALANCED"

    # Book thickness classification
    if total_depth < 500:
        result["book_class"] = "THIN"
        result["book_note"] = "Easy to move price; high slippage risk"
    elif total_depth < 5000:
        result["book_class"] = "MODERATE"
        result["book_note"] = "Normal depth; moderate slippage on large orders"
    else:
        result["book_class"] = "THICK"
        result["book_note"] = "Stable book; low slippage for most order sizes"

    # Bid levels detail
    result["bid_levels"] = [
        {"price": p, "size": round(s, 2), "cumulative": round(sum(sz for _, sz in top_bids[:i+1]), 2)}
        for i, (p, s) in enumerate(top_bids)
    ]
    result["ask_levels"] = [
        {"price": p, "size": round(s, 2), "cumulative": round(sum(sz for _, sz in top_asks[:i+1]), 2)}
        for i, (p, s) in enumerate(top_asks)
    ]

    # Slippage estimate: cost to buy/sell $100 worth
    slippage_size = 100.0
    result["buy_slippage"] = _estimate_slippage(asks, slippage_size)
    result["sell_slippage"] = _estimate_slippage(
        [(p, s) for p, s in bids], slippage_size, selling=True
    )

    return result


def _estimate_slippage(
    levels: list[tuple[float, float]], target_size: float, selling: bool = False
) -> dict | None:
    """Estimate average fill price and slippage for a target size."""
    if not levels:
        return None

    filled = 0.0
    cost = 0.0

    for price, size in levels:
        remaining = target_size - filled
        fill_qty = min(size, remaining)
        cost += fill_qty * price
        filled += fill_qty
        if filled >= target_size:
            break

    if filled == 0:
        return None

    avg_price = cost / filled
    best_price = levels[0][0]
    slippage = abs(avg_price - best_price)

    return {
        "target_size": target_size,
        "filled": round(filled, 2),
        "avg_price": round(avg_price, 6),
        "best_price": best_price,
        "slippage": round(slippage, 6),
        "slippage_pct": round(slippage / best_price * 100 if best_price else 0, 4),
        "fully_filled": filled >= target_size,
    }


def format_output(result: dict) -> str:
    """Format analysis result for display."""
    lines = []
    lines.append(f"Order Book Analysis for {result['token_id'][:30]}...")
    lines.append("=" * 70)

    if result.get("status") == "EMPTY_BOOK":
        lines.append("Order book is empty -- no bids or asks.")
        return "\n".join(lines)

    lines.append(f"  Best Bid:    ${result['best_bid']:.4f}")
    lines.append(f"  Best Ask:    ${result['best_ask']:.4f}")
    lines.append(f"  Mid Price:   ${result['mid_price']:.4f}" if result['mid_price'] else "  Mid Price:   N/A")
    lines.append(f"  Spread:      ${result['spread']:.4f} ({result['spread_pct']:.2f}%)")
    lines.append("")

    lines.append(f"Depth (top {result['depth_levels_used']} levels):")
    lines.append(f"  Bid Depth:   {result['bid_depth']:,.2f} shares")
    lines.append(f"  Ask Depth:   {result['ask_depth']:,.2f} shares")
    lines.append(f"  Total:       {result['total_depth']:,.2f} shares")
    lines.append(f"  Imbalance:   {result['imbalance_ratio']:+.4f} ({result['imbalance_signal']})")
    lines.append(f"  Book Class:  {result['book_class']} -- {result['book_note']}")
    lines.append("")

    lines.append("Bid Levels:")
    lines.append(f"  {'Price':>8}  {'Size':>10}  {'Cumulative':>12}")
    for lvl in result.get("bid_levels", []):
        lines.append(f"  ${lvl['price']:<7.4f}  {lvl['size']:>10,.2f}  {lvl['cumulative']:>12,.2f}")

    lines.append("")
    lines.append("Ask Levels:")
    lines.append(f"  {'Price':>8}  {'Size':>10}  {'Cumulative':>12}")
    for lvl in result.get("ask_levels", []):
        lines.append(f"  ${lvl['price']:<7.4f}  {lvl['size']:>10,.2f}  {lvl['cumulative']:>12,.2f}")

    for label, key in [("Buy", "buy_slippage"), ("Sell", "sell_slippage")]:
        slip = result.get(key)
        lines.append("")
        if slip:
            status = "YES" if slip["fully_filled"] else "PARTIAL"
            lines.append(
                f"{label} Slippage ({slip['target_size']:.0f} shares): "
                f"avg ${slip['avg_price']:.4f}, "
                f"slippage ${slip['slippage']:.4f} ({slip['slippage_pct']:.2f}%), "
                f"filled: {status}"
            )
        else:
            lines.append(f"{label} Slippage: No liquidity on this side")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Polymarket order book"
    )
    parser.add_argument(
        "--token-id",
        required=True,
        help="CLOB token ID to analyze",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=5,
        help="Number of price levels to analyze (default: 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    try:
        book = fetch_orderbook(args.token_id)
    except Exception as e:
        print(f"Error fetching order book: {e}", file=sys.stderr)
        sys.exit(1)

    result = analyze(book, depth=args.depth)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_output(result))


if __name__ == "__main__":
    main()
