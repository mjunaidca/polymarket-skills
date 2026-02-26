#!/usr/bin/env python3
"""Scan and search active Polymarket prediction markets via the Gamma API."""

import argparse
import json
import re
import sys

import requests

GAMMA_API = "https://gamma-api.polymarket.com"

MAX_TEXT_LEN = 200


def sanitize_text(text):
    """Strip control characters and limit length. Market text is user-generated."""
    if not text:
        return ""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "..."
    return text


def fetch_markets(limit=20, category=None, search=None, min_volume=0,
                  sort_by="volume24hr", ascending=False):
    """Fetch active markets from Gamma API with filtering and sorting."""
    params = {
        "limit": min(limit, 100),
        "active": "true",
        "closed": "false",
        "order": sort_by,
        "ascending": str(ascending).lower(),
    }

    if category:
        params["tag_slug"] = category.lower()

    resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=30)
    resp.raise_for_status()
    raw_markets = resp.json()

    results = []
    for m in raw_markets:
        vol_24h = float(m.get("volume24hr", 0) or 0)
        if vol_24h < min_volume:
            continue

        # Parse JSON-encoded fields
        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
        except (json.JSONDecodeError, TypeError):
            outcomes = []

        try:
            outcome_prices = json.loads(m.get("outcomePrices", "[]"))
            outcome_prices = [float(p) for p in outcome_prices]
        except (json.JSONDecodeError, TypeError, ValueError):
            outcome_prices = []

        try:
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
        except (json.JSONDecodeError, TypeError):
            token_ids = []

        # Apply keyword search filter
        if search:
            question = (m.get("question", "") or "").lower()
            description = (m.get("description", "") or "").lower()
            search_lower = search.lower()
            if search_lower not in question and search_lower not in description:
                continue

        market = {
            "question": sanitize_text(m.get("question", "")),
            "slug": m.get("slug", ""),
            "url": f"https://polymarket.com/event/{m.get('slug', '')}",
            "outcomes": [sanitize_text(o) for o in outcomes],
            "outcome_prices": outcome_prices,
            "token_ids": token_ids,
            "volume_24h": vol_24h,
            "volume_total": float(m.get("volumeNum", 0) or 0),
            "liquidity": float(m.get("liquidityNum", 0) or 0),
            "end_date": m.get("endDate", ""),
            "active": m.get("active", False),
            "accepting_orders": m.get("acceptingOrders", False),
        }
        results.append(market)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Scan active Polymarket prediction markets"
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Number of markets to return (max 100, default 20)"
    )
    parser.add_argument(
        "--category", type=str, default=None,
        help="Filter by tag/category (e.g. crypto, politics, sports)"
    )
    parser.add_argument(
        "--search", type=str, default=None,
        help="Search keyword in market question/description"
    )
    parser.add_argument(
        "--min-volume", type=float, default=0,
        help="Minimum 24h volume in USD (default 0)"
    )
    parser.add_argument(
        "--sort-by", type=str, default="volume24hr",
        choices=["volume24hr", "liquidity", "endDate", "startDate"],
        help="Sort field (default: volume24hr)"
    )
    parser.add_argument(
        "--ascending", action="store_true",
        help="Sort ascending instead of descending"
    )

    args = parser.parse_args()

    try:
        markets = fetch_markets(
            limit=args.limit,
            category=args.category,
            search=args.search,
            min_volume=args.min_volume,
            sort_by=args.sort_by,
            ascending=args.ascending,
        )
        print(json.dumps(markets, indent=2))
    except requests.RequestException as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
