#!/usr/bin/env python3
"""Monitor multiple Polymarket tokens for significant price changes."""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

CLOB_HOST = "https://clob.polymarket.com"


def poll_prices(client, token_ids):
    """Fetch current midpoints for all token IDs."""
    if len(token_ids) == 1:
        tid = token_ids[0]
        mid = client.get_midpoint(tid)
        spread = client.get_spread(tid)
        return {tid: {"midpoint": float(mid.get("mid", 0)),
                       "spread": float(spread.get("spread", 0))}}

    params = [BookParams(token_id=tid) for tid in token_ids]
    midpoints = client.get_midpoints(params)
    spreads = client.get_spreads(params)

    results = {}
    for tid in token_ids:
        mid_val = midpoints.get(tid, "0")
        spread_val = spreads.get(tid, "0")
        results[tid] = {
            "midpoint": float(mid_val) if mid_val else 0.0,
            "spread": float(spread_val) if spread_val else 0.0,
        }
    return results


def run_monitor(token_ids, interval, threshold, max_polls, baseline_window):
    """Main monitoring loop."""
    client = ClobClient(CLOB_HOST)
    price_history = {tid: [] for tid in token_ids}
    poll_count = 0

    while True:
        poll_count += 1
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            current = poll_prices(client, token_ids)
        except Exception as e:
            print(json.dumps({"type": "error", "timestamp": now,
                              "message": str(e)}), file=sys.stderr)
            time.sleep(interval)
            continue

        alerts_this_poll = 0
        for tid in token_ids:
            data = current.get(tid)
            if not data:
                continue

            price = data["midpoint"]
            spread = data["spread"]
            history = price_history[tid]
            history.append(price)

            # Need at least 2 data points to compare
            if len(history) < 2:
                continue

            # Calculate baseline from window
            window = history[-(baseline_window + 1):-1]
            if not window:
                continue
            baseline = sum(window) / len(window)

            if baseline == 0:
                continue

            change_pct = ((price - baseline) / baseline) * 100

            if abs(change_pct) >= threshold:
                alert = {
                    "type": "price_alert",
                    "token_id": tid,
                    "timestamp": now,
                    "current_price": price,
                    "baseline_price": round(baseline, 6),
                    "change_pct": round(change_pct, 2),
                    "direction": "up" if change_pct > 0 else "down",
                    "spread": spread,
                    "poll_number": poll_count,
                }
                print(json.dumps(alert), flush=True)
                alerts_this_poll += 1

        if alerts_this_poll == 0:
            print(f"[poll {poll_count}] {now} — no alerts "
                  f"(monitoring {len(token_ids)} tokens, "
                  f"threshold={threshold}%)", file=sys.stderr, flush=True)

        if max_polls and poll_count >= max_polls:
            print(json.dumps({"type": "monitor_complete",
                              "total_polls": poll_count,
                              "timestamp": now}), flush=True)
            break

        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor Polymarket tokens for price changes"
    )
    parser.add_argument(
        "--token-id", type=str, action="append", required=True,
        help="CLOB token ID to monitor (repeatable)"
    )
    parser.add_argument(
        "--interval", type=int, default=30,
        help="Polling interval in seconds (default 30, min 5)"
    )
    parser.add_argument(
        "--threshold", type=float, default=5.0,
        help="Percentage change to trigger alert (default 5.0)"
    )
    parser.add_argument(
        "--max-polls", type=int, default=0,
        help="Stop after N polls (default 0 = unlimited)"
    )
    parser.add_argument(
        "--baseline-window", type=int, default=1,
        help="Number of recent prices to average for baseline (default 1)"
    )

    args = parser.parse_args()
    interval = max(args.interval, 5)

    run_monitor(
        token_ids=args.token_id,
        interval=interval,
        threshold=args.threshold,
        max_polls=args.max_polls if args.max_polls > 0 else None,
        baseline_window=max(args.baseline_window, 1),
    )


if __name__ == "__main__":
    main()
