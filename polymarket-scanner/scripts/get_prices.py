#!/usr/bin/env python3
"""Fetch current prices, midpoints, and spreads for Polymarket tokens."""

import argparse
import json
import sys

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


def resolve_slug_to_token_ids(slug):
    """Look up a market by slug and return its token IDs."""
    resp = requests.get(
        f"{GAMMA_API}/markets",
        params={"slug": slug, "limit": 1},
        timeout=30,
    )
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        return []
    market = markets[0]
    try:
        return json.loads(market.get("clobTokenIds", "[]"))
    except (json.JSONDecodeError, TypeError):
        return []


def fetch_prices(token_ids):
    """Fetch prices for a list of token IDs using the CLOB API."""
    client = ClobClient(CLOB_HOST)

    if len(token_ids) == 1:
        tid = token_ids[0]
        mid = client.get_midpoint(tid)
        spread = client.get_spread(tid)
        last = client.get_last_trade_price(tid)
        buy_price = client.get_price(tid, "BUY")
        sell_price = client.get_price(tid, "SELL")

        return [{
            "token_id": tid,
            "midpoint": float(mid.get("mid", 0)),
            "best_bid": float(buy_price.get("price", 0)),
            "best_ask": float(sell_price.get("price", 0)),
            "spread": float(spread.get("spread", 0)),
            "last_trade_price": float(last.get("price", 0)),
            "last_trade_side": last.get("side", ""),
        }]

    # Batch mode for multiple tokens
    params = [BookParams(token_id=tid) for tid in token_ids]
    midpoints = client.get_midpoints(params)
    spreads = client.get_spreads(params)
    last_trades_raw = client.get_last_trades_prices(params)

    # last_trades_prices returns a list of dicts with token_id key, not a dict
    last_trades_by_id = {}
    if isinstance(last_trades_raw, list):
        for item in last_trades_raw:
            if isinstance(item, dict) and "token_id" in item:
                last_trades_by_id[item["token_id"]] = item
    elif isinstance(last_trades_raw, dict):
        last_trades_by_id = last_trades_raw

    results = []
    for tid in token_ids:
        mid_val = midpoints.get(tid, "0")
        spread_val = spreads.get(tid, "0")
        last_info = last_trades_by_id.get(tid, {})

        # Get individual bid/ask prices
        try:
            buy_price = client.get_price(tid, "BUY")
            sell_price = client.get_price(tid, "SELL")
            best_bid = float(buy_price.get("price", 0))
            best_ask = float(sell_price.get("price", 0))
        except Exception:
            best_bid = 0.0
            best_ask = 0.0

        results.append({
            "token_id": tid,
            "midpoint": float(mid_val) if mid_val else 0.0,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": float(spread_val) if spread_val else 0.0,
            "last_trade_price": float(last_info.get("price", 0)) if isinstance(last_info, dict) else 0.0,
            "last_trade_side": last_info.get("side", "") if isinstance(last_info, dict) else "",
        })

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Get current prices for Polymarket tokens"
    )
    parser.add_argument(
        "--token-id", type=str, action="append", default=None,
        help="CLOB token ID (can be specified multiple times)"
    )
    parser.add_argument(
        "--market-slug", type=str, default=None,
        help="Market slug to look up token IDs automatically"
    )

    args = parser.parse_args()

    token_ids = args.token_id or []

    if args.market_slug:
        slug_ids = resolve_slug_to_token_ids(args.market_slug)
        if not slug_ids:
            print(json.dumps({"error": f"No tokens found for slug: {args.market_slug}"}),
                  file=sys.stderr)
            sys.exit(1)
        token_ids.extend(slug_ids)

    if not token_ids:
        print(json.dumps({"error": "Provide --token-id or --market-slug"}),
              file=sys.stderr)
        sys.exit(1)

    try:
        results = fetch_prices(token_ids)
        print(json.dumps(results, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
