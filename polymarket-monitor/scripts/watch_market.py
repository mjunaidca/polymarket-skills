#!/usr/bin/env python3
"""Continuously watch a single Polymarket token with detailed snapshots."""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from py_clob_client.client import ClobClient

CLOB_HOST = "https://clob.polymarket.com"


def take_snapshot(client, token_id, poll_number):
    """Capture a full market snapshot for a single token."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    mid = client.get_midpoint(token_id)
    spread = client.get_spread(token_id)
    last = client.get_last_trade_price(token_id)

    # Get order book for depth info
    ob = client.get_order_book(token_id)
    bids = [{"price": float(b.price), "size": float(b.size)} for b in ob.bids]
    asks = [{"price": float(a.price), "size": float(a.size)} for a in ob.asks]

    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])

    best_bid = bids[0]["price"] if bids else 0.0
    best_ask = asks[0]["price"] if asks else 1.0
    bid_depth = round(sum(b["size"] for b in bids), 2)
    ask_depth = round(sum(a["size"] for a in asks), 2)

    return {
        "type": "market_snapshot",
        "token_id": token_id,
        "timestamp": now,
        "midpoint": float(mid.get("mid", 0)),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": float(spread.get("spread", 0)),
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "last_trade_price": float(last.get("price", 0)),
        "last_trade_side": last.get("side", ""),
        "poll_number": poll_number,
    }


def run_watch(token_id, interval, max_polls):
    """Main watch loop."""
    client = ClobClient(CLOB_HOST)
    poll_count = 0

    while True:
        poll_count += 1

        try:
            snapshot = take_snapshot(client, token_id, poll_count)
            print(json.dumps(snapshot), flush=True)
        except Exception as e:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(json.dumps({"type": "error", "timestamp": now,
                              "message": str(e)}), file=sys.stderr, flush=True)

        if max_polls and poll_count >= max_polls:
            break

        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(
        description="Watch a single Polymarket token with detailed snapshots"
    )
    parser.add_argument(
        "--token-id", type=str, required=True,
        help="CLOB token ID to watch"
    )
    parser.add_argument(
        "--interval", type=int, default=15,
        help="Snapshot interval in seconds (default 15, min 5)"
    )
    parser.add_argument(
        "--max-polls", type=int, default=0,
        help="Stop after N snapshots (default 0 = unlimited)"
    )

    args = parser.parse_args()
    interval = max(args.interval, 5)

    run_watch(
        token_id=args.token_id,
        interval=interval,
        max_polls=args.max_polls if args.max_polls > 0 else None,
    )


if __name__ == "__main__":
    main()
