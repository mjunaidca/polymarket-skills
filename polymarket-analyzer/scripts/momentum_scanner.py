#!/usr/bin/env python3
"""Scan Polymarket for momentum signals: volume surges and price trends.

Detects:
  - Volume surges: 24h volume significantly exceeds 7-day daily average
  - Price momentum: markets with strong directional price movement
  - Liquidity anomalies: unusually high or low liquidity relative to volume

Uses Gamma API (no auth required).
"""

import argparse
import json
import sys

import requests


GAMMA_API = "https://gamma-api.polymarket.com"


def fetch_markets(limit: int = 100, offset: int = 0) -> list[dict]:
    """Fetch active markets from Gamma API."""
    url = (
        f"{GAMMA_API}/markets"
        f"?limit={limit}&offset={offset}&active=true&closed=false"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def compute_signals(market: dict) -> dict | None:
    """Compute momentum signals for a single market."""
    vol_24h = float(market.get("volume24hr", 0) or 0)
    vol_1wk = float(market.get("volume1wk", 0) or 0)
    liquidity = float(market.get("liquidityNum", 0) or 0)

    # Need at least some volume data
    if vol_24h <= 0 and vol_1wk <= 0:
        return None

    # Parse prices
    raw_prices = market.get("outcomePrices")
    if not raw_prices:
        return None
    try:
        prices = json.loads(raw_prices)
        yes_price = float(prices[0])
    except (json.JSONDecodeError, ValueError, IndexError):
        return None

    # Volume surge: compare 24h volume to 7-day daily average
    daily_avg_7d = vol_1wk / 7.0 if vol_1wk > 0 else 0
    if daily_avg_7d > 0:
        volume_ratio = vol_24h / daily_avg_7d
    else:
        volume_ratio = 0.0

    # Price extremity: how far from 0.50 (max uncertainty)
    # Prices near 0 or 1 suggest strong directional conviction
    price_extremity = abs(yes_price - 0.5) * 2.0  # 0 at 0.50, 1 at 0 or 1

    # Volume-to-liquidity ratio: high ratio suggests heavy activity relative to depth
    vol_liq_ratio = vol_24h / liquidity if liquidity > 0 else 0

    # Composite momentum score
    # volume_ratio contributes most -- a surge is the primary signal
    score = 0.0
    if volume_ratio > 1.0:
        score += min((volume_ratio - 1.0) * 0.4, 2.0)  # Cap contribution at 2.0
    if vol_liq_ratio > 1.0:
        score += min((vol_liq_ratio - 1.0) * 0.3, 1.5)
    # Extreme prices amplify the signal (market is moving toward resolution)
    if price_extremity > 0.6:
        score += (price_extremity - 0.6) * 0.3

    if score <= 0:
        return None

    # Classify the signal
    if volume_ratio >= 3.0:
        volume_signal = "VOLUME_SURGE"
    elif volume_ratio >= 1.5:
        volume_signal = "ELEVATED_VOLUME"
    else:
        volume_signal = "NORMAL_VOLUME"

    if yes_price >= 0.85:
        direction = "STRONG_YES"
    elif yes_price >= 0.65:
        direction = "LEANING_YES"
    elif yes_price <= 0.15:
        direction = "STRONG_NO"
    elif yes_price <= 0.35:
        direction = "LEANING_NO"
    else:
        direction = "NEUTRAL"

    return {
        "question": market.get("question", "Unknown"),
        "slug": market.get("slug", ""),
        "yes_price": yes_price,
        "direction": direction,
        "volume_24h": round(vol_24h, 2),
        "daily_avg_7d": round(daily_avg_7d, 2),
        "volume_ratio": round(volume_ratio, 2),
        "volume_signal": volume_signal,
        "liquidity": round(liquidity, 2),
        "vol_liq_ratio": round(vol_liq_ratio, 2),
        "momentum_score": round(score, 4),
    }


def scan_momentum(
    max_markets: int = 300,
    min_volume: float = 1000.0,
    min_score: float = 0.1,
) -> list[dict]:
    """Scan markets and rank by momentum score."""
    signals = []
    offset = 0
    batch_size = 100
    fetched = 0

    while fetched < max_markets:
        batch = fetch_markets(limit=batch_size, offset=offset)
        if not batch:
            break

        for market in batch:
            vol_24h = float(market.get("volume24hr", 0) or 0)
            if vol_24h < min_volume:
                continue

            sig = compute_signals(market)
            if sig and sig["momentum_score"] >= min_score:
                signals.append(sig)

        fetched += len(batch)
        offset += batch_size

        if len(batch) < batch_size:
            break

    # Rank by momentum score descending
    signals.sort(key=lambda x: x["momentum_score"], reverse=True)
    return signals


def format_output(signals: list[dict]) -> str:
    """Format momentum signals for display."""
    if not signals:
        return "No momentum signals found matching criteria."

    lines = []
    lines.append(f"Found {len(signals)} market(s) with momentum signals:\n")
    lines.append(
        f"{'Score':>6}  {'YES':>5}  {'Direction':<12} "
        f"{'VolRatio':>8}  {'Signal':<16} "
        f"{'Vol24h':>12}  {'Avg7d':>10}  Question"
    )
    lines.append("-" * 120)

    for s in signals:
        lines.append(
            f"{s['momentum_score']:>6.2f}  "
            f"${s['yes_price']:<4.2f}  "
            f"{s['direction']:<12} "
            f"{s['volume_ratio']:>7.1f}x  "
            f"{s['volume_signal']:<16} "
            f"${s['volume_24h']:>11,.0f}  "
            f"${s['daily_avg_7d']:>9,.0f}  "
            f"{s['question'][:55]}"
        )

    lines.append("")
    lines.append("Score = composite of volume surge, vol/liquidity ratio, and price extremity.")
    lines.append("Volume Ratio = 24h volume / 7-day daily average (>3x = VOLUME_SURGE).")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Scan Polymarket for momentum signals"
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=1000,
        help="Minimum 24h volume to consider (default: $1,000)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.1,
        help="Minimum momentum score to report (default: 0.1)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=300,
        help="Maximum number of markets to scan (default: 300)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    try:
        signals = scan_momentum(
            max_markets=args.limit,
            min_volume=args.min_volume,
            min_score=args.min_score,
        )
    except requests.RequestException as e:
        print(f"Error fetching data from Gamma API: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(signals, indent=2))
    else:
        print(format_output(signals))


if __name__ == "__main__":
    main()
