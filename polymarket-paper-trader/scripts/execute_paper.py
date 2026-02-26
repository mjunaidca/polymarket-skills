#!/usr/bin/env python3
"""
Paper Trade Executor

Higher-level wrapper around paper_engine that takes structured trade
recommendations (e.g., from a strategy advisor) and executes them as
paper trades with full validation.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Import from paper_engine (same directory)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.append(_THIS_DIR)
from paper_engine import (
    get_portfolio,
    place_order,
    close_position,
    fetch_midpoint,
    DEFAULT_FEE_RATE,
)


def execute_recommendation(
    recommendation: dict,
    portfolio_name: str = "default",
    dry_run: bool = False,
) -> dict:
    """
    Execute a trade recommendation from a strategy advisor.

    Expected recommendation format:
    {
        "token_id": "...",
        "side": "YES" or "NO",
        "action": "BUY" or "SELL" or "CLOSE",
        "size_usd": 50.0,           # USD amount (for BUY)
        "size_pct": 0.05,           # OR as % of portfolio (alternative to size_usd)
        "price": 0.45,              # optional limit price
        "confidence": 0.75,         # strategy confidence 0-1
        "reasoning": "...",         # why this trade
        "strategy": "momentum",    # which strategy generated this
        "fee_rate": 0.0,           # optional fee override
    }

    Returns execution result dict.
    """
    token_id = recommendation.get("token_id")
    if not token_id:
        return {"status": "rejected", "reason": "Missing token_id"}

    action = recommendation.get("action", "BUY").upper()
    side = recommendation.get("side", "YES").upper()
    confidence = recommendation.get("confidence", 0.5)
    reasoning = recommendation.get("reasoning", "")
    strategy = recommendation.get("strategy", "unknown")
    fee_rate = recommendation.get("fee_rate", DEFAULT_FEE_RATE)
    price = recommendation.get("price")

    # Build reasoning string
    full_reasoning = f"[{strategy}] (conf={confidence:.0%}) {reasoning}"

    # Get current portfolio state
    try:
        portfolio = get_portfolio(portfolio_name, refresh_prices=True)
    except RuntimeError as exc:
        return {"status": "rejected", "reason": str(exc)}

    # Confidence gate
    min_confidence = 0.5
    if confidence < min_confidence:
        return {
            "status": "rejected",
            "reason": f"Confidence {confidence:.0%} below minimum {min_confidence:.0%}",
            "recommendation": recommendation,
        }

    # Handle CLOSE action
    if action == "CLOSE":
        if dry_run:
            return {
                "status": "dry_run",
                "action": "CLOSE",
                "token_id": token_id,
                "side": side,
                "portfolio": _summary(portfolio),
            }
        try:
            result = close_position(
                token_id=token_id,
                side=side if side in ("YES", "NO") else None,
                portfolio_name=portfolio_name,
                fee_rate=fee_rate,
                reasoning=full_reasoning,
            )
            return {
                "status": "executed",
                "action": "CLOSE",
                "result": result,
                "portfolio": _summary(
                    get_portfolio(portfolio_name, refresh_prices=False)
                ),
            }
        except RuntimeError as exc:
            return {"status": "rejected", "reason": str(exc)}

    # Determine size in USD
    size_usd = recommendation.get("size_usd")
    size_pct = recommendation.get("size_pct")

    if size_usd is None and size_pct is not None:
        size_usd = portfolio["total_value"] * size_pct
    elif size_usd is None:
        # Default: Kelly-inspired sizing based on confidence
        # Half-Kelly: f = (2p - 1) where p = confidence, then halved
        kelly_fraction = max(0, (2 * confidence - 1)) * 0.5
        # Cap at 10% of portfolio
        kelly_fraction = min(kelly_fraction, 0.10)
        size_usd = portfolio["total_value"] * kelly_fraction

    if size_usd <= 0:
        return {
            "status": "rejected",
            "reason": "Calculated trade size is zero (confidence too low for Kelly sizing)",
        }

    # Round to 2 decimal places
    size_usd = round(size_usd, 2)

    # Get current market price for context
    try:
        current_price = fetch_midpoint(token_id)
    except Exception:
        current_price = None

    if dry_run:
        return {
            "status": "dry_run",
            "action": action,
            "side": side,
            "token_id": token_id,
            "size_usd": size_usd,
            "limit_price": price,
            "current_price": current_price,
            "confidence": confidence,
            "strategy": strategy,
            "reasoning": full_reasoning,
            "portfolio": _summary(portfolio),
        }

    # Execute the trade
    try:
        result = place_order(
            token_id=token_id,
            side=side,
            size=size_usd,
            price=price,
            reasoning=full_reasoning,
            portfolio_name=portfolio_name,
            fee_rate=fee_rate,
        )
        # Get updated portfolio
        updated = get_portfolio(portfolio_name, refresh_prices=False)
        return {
            "status": "executed",
            "action": action,
            "result": result,
            "portfolio": _summary(updated),
        }
    except RuntimeError as exc:
        return {
            "status": "rejected",
            "reason": str(exc),
            "attempted": {
                "token_id": token_id,
                "side": side,
                "size_usd": size_usd,
                "price": price,
            },
        }


def _summary(portfolio: dict) -> dict:
    """Compact portfolio summary for trade results."""
    return {
        "total_value": portfolio["total_value"],
        "cash_balance": portfolio["cash_balance"],
        "pnl": portfolio["pnl"],
        "pnl_pct": portfolio["pnl_pct"],
        "num_positions": portfolio["num_open_positions"],
    }


def execute_batch(
    recommendations: list[dict],
    portfolio_name: str = "default",
    dry_run: bool = False,
) -> list[dict]:
    """Execute a batch of recommendations sequentially."""
    results = []
    for rec in recommendations:
        result = execute_recommendation(rec, portfolio_name, dry_run)
        results.append(result)
        # Stop on risk limit errors
        if result["status"] == "rejected" and "drawdown" in result.get("reason", ""):
            for remaining in recommendations[len(results):]:
                results.append({
                    "status": "skipped",
                    "reason": "Trading halted due to drawdown limit",
                    "recommendation": remaining,
                })
            break
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Execute paper trade recommendations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Execute a single recommendation from JSON string
  %(prog)s --recommendation '{"token_id":"ABC","side":"YES","size_usd":50,"confidence":0.8}'

  # Execute from a JSON file
  %(prog)s --file recommendations.json

  # Dry run (no actual trades)
  %(prog)s --recommendation '{"token_id":"ABC","side":"YES","size_usd":50}' --dry-run
        """,
    )
    parser.add_argument("--recommendation", help="JSON trade recommendation")
    parser.add_argument("--file", help="JSON file with recommendation(s)")
    parser.add_argument("--portfolio", default="default", help="Portfolio name")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate without executing")
    parser.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args()

    if not args.recommendation and not args.file:
        parser.error("Provide --recommendation or --file")

    try:
        if args.file:
            with open(args.file) as f:
                data = json.load(f)
            if isinstance(data, list):
                results = execute_batch(data, args.portfolio, args.dry_run)
            else:
                results = [execute_recommendation(data, args.portfolio, args.dry_run)]
        else:
            rec = json.loads(args.recommendation)
            if isinstance(rec, list):
                results = execute_batch(rec, args.portfolio, args.dry_run)
            else:
                results = [execute_recommendation(rec, args.portfolio, args.dry_run)]

        if args.json:
            print(json.dumps(results if len(results) > 1 else results[0], indent=2))
        else:
            for r in results:
                status = r["status"].upper()
                if r["status"] == "executed":
                    res = r["result"]
                    if isinstance(res, dict):
                        print(
                            f"[{status}] {res.get('action','?')} {res.get('side','?')} "
                            f"{res.get('shares', 0):.2f} shares @ "
                            f"${res.get('avg_price', res.get('avg_sell_price', 0)):.4f}"
                        )
                    else:
                        print(f"[{status}] {json.dumps(res)}")
                    pf = r.get("portfolio", {})
                    print(
                        f"  Portfolio: ${pf.get('total_value', 0):,.2f} "
                        f"({pf.get('pnl_pct', 0):+.2f}%)"
                    )
                elif r["status"] == "dry_run":
                    print(
                        f"[DRY RUN] Would {r.get('action','?')} "
                        f"{r.get('side','?')} ${r.get('size_usd', 0):.2f} "
                        f"(price: {r.get('current_price', '?')})"
                    )
                else:
                    print(f"[{status}] {r.get('reason', 'Unknown error')}")

    except (json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
