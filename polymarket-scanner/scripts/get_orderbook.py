#!/usr/bin/env python3
"""Fetch the full order book for a Polymarket token from the CLOB API."""

import argparse
import json
import sys

from py_clob_client.client import ClobClient

CLOB_HOST = "https://clob.polymarket.com"


def fetch_orderbook(token_id, depth=10):
    """Fetch order book for a token and return structured data."""
    client = ClobClient(CLOB_HOST)
    ob = client.get_order_book(token_id)

    bids = [{"price": float(b.price), "size": float(b.size)} for b in ob.bids]
    asks = [{"price": float(a.price), "size": float(a.size)} for a in ob.asks]

    # Sort: bids descending by price, asks ascending by price
    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])

    best_bid = bids[0]["price"] if bids else 0.0
    best_ask = asks[0]["price"] if asks else 1.0
    spread = round(best_ask - best_bid, 6)
    midpoint = round((best_ask + best_bid) / 2, 6)

    bid_depth = round(sum(b["size"] for b in bids), 2)
    ask_depth = round(sum(a["size"] for a in asks), 2)

    return {
        "market": ob.market,
        "asset_id": ob.asset_id,
        "bids": bids[:depth],
        "asks": asks[:depth],
        "spread": spread,
        "midpoint": midpoint,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "total_bid_levels": len(bids),
        "total_ask_levels": len(asks),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fetch order book for a Polymarket token"
    )
    parser.add_argument(
        "--token-id", type=str, required=True,
        help="CLOB token ID (from scan_markets.py output)"
    )
    parser.add_argument(
        "--depth", type=int, default=10,
        help="Number of price levels to show (default 10)"
    )

    args = parser.parse_args()

    try:
        result = fetch_orderbook(args.token_id, depth=args.depth)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
