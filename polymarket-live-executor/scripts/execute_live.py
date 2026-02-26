#!/usr/bin/env python3
"""Execute live trades on Polymarket with mandatory human confirmation.

SAFETY: Every trade requires interactive confirmation. No autonomous execution.

Requires environment variables:
  POLYMARKET_PRIVATE_KEY   - Burner wallet private key (NEVER main wallet)
  POLYMARKET_CONFIRM=true  - Safety gate (must be exactly "true")

Optional environment variables:
  POLYMARKET_MAX_SIZE         - Max $ per trade (default: 10)
  POLYMARKET_DAILY_LOSS_LIMIT - Max daily loss in $ (default: 50)

All trades are logged to ~/.polymarket-live/trades.log
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    OrderArgs,
    MarketOrderArgs,
    OrderType,
)


CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
LOG_DIR = Path.home() / ".polymarket-live"
LOG_FILE = LOG_DIR / "trades.log"


def check_safety_gates() -> tuple[bool, str]:
    """Verify all safety gates are in place. Returns (ok, message)."""
    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not key:
        return False, (
            "POLYMARKET_PRIVATE_KEY not set.\n"
            "Set it to your BURNER wallet private key (never your main wallet).\n"
            "See references/security.md for setup instructions."
        )

    confirm = os.environ.get("POLYMARKET_CONFIRM", "")
    if confirm != "true":
        return False, (
            "POLYMARKET_CONFIRM is not set to 'true'.\n"
            "This safety gate prevents accidental trade execution.\n"
            "Set POLYMARKET_CONFIRM=true when you are ready for live trading."
        )

    return True, "OK"


def get_max_size() -> float:
    """Get maximum position size from env or default."""
    try:
        return float(os.environ.get("POLYMARKET_MAX_SIZE", "10"))
    except ValueError:
        return 10.0


def get_daily_loss_limit() -> float:
    """Get daily loss limit from env or default."""
    try:
        return float(os.environ.get("POLYMARKET_DAILY_LOSS_LIMIT", "50"))
    except ValueError:
        return 50.0


def get_daily_spending() -> float:
    """Calculate total spending today from the trade log."""
    if not LOG_FILE.exists():
        return 0.0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0

    for line in LOG_FILE.read_text().splitlines():
        try:
            entry = json.loads(line)
            if entry.get("timestamp", "").startswith(today) and entry.get("status") == "EXECUTED":
                total += float(entry.get("cost_usd", 0))
        except (json.JSONDecodeError, ValueError):
            continue

    return total


def log_trade(entry: dict):
    """Append a trade entry to the log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def create_authenticated_client() -> ClobClient:
    """Create an L2-authenticated ClobClient."""
    key = os.environ["POLYMARKET_PRIVATE_KEY"]

    # Initialize L1 client
    client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=key)

    # Derive L2 API credentials
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    return client


def get_orderbook_context(client: ClobClient, token_id: str) -> dict:
    """Fetch order book context for display."""
    try:
        book = client.get_order_book(token_id)
        bids = [(float(b.price), float(b.size)) for b in (book.bids or [])]
        asks = [(float(a.price), float(a.size)) for a in (book.asks or [])]
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        return {
            "best_bid": bids[0] if bids else None,
            "best_ask": asks[0] if asks else None,
            "bid_depth_5": sum(s for _, s in bids[:5]),
            "ask_depth_5": sum(s for _, s in asks[:5]),
            "spread": (asks[0][0] - bids[0][0]) if (bids and asks) else None,
        }
    except Exception as e:
        return {"error": str(e)}


def display_trade_confirmation(
    side: str,
    token_id: str,
    size: float | None,
    amount: float | None,
    price: float | None,
    is_market: bool,
    context: dict,
    max_size: float,
    daily_spent: float,
    daily_limit: float,
) -> str:
    """Display full trade details and return confirmation prompt text."""
    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append("  LIVE TRADE CONFIRMATION REQUIRED")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"  Side:       {side}")
    lines.append(f"  Token ID:   {token_id[:40]}...")

    if is_market:
        lines.append(f"  Order Type: MARKET (Fill-or-Kill)")
        lines.append(f"  Amount:     ${amount:.2f} USD")
    else:
        lines.append(f"  Order Type: LIMIT (Good-til-Cancelled)")
        lines.append(f"  Size:       {size:.2f} shares")
        lines.append(f"  Price:      ${price:.4f} per share")
        est_cost = size * price
        lines.append(f"  Est. Cost:  ${est_cost:.2f} USD")

    lines.append("")
    lines.append("  Order Book Context:")
    if "error" in context:
        lines.append(f"    ERROR: {context['error']}")
    else:
        bb = context.get("best_bid")
        ba = context.get("best_ask")
        lines.append(f"    Best Bid:  ${bb[0]:.4f} ({bb[1]:.0f} shares)" if bb else "    Best Bid:  N/A")
        lines.append(f"    Best Ask:  ${ba[0]:.4f} ({ba[1]:.0f} shares)" if ba else "    Best Ask:  N/A")
        spread = context.get("spread")
        lines.append(f"    Spread:    ${spread:.4f}" if spread is not None else "    Spread:    N/A")
        lines.append(f"    Bid Depth (5 lvls): {context.get('bid_depth_5', 0):,.0f} shares")
        lines.append(f"    Ask Depth (5 lvls): {context.get('ask_depth_5', 0):,.0f} shares")

    lines.append("")
    lines.append("  Risk Controls:")
    lines.append(f"    Max trade size:  ${max_size:.2f}")
    lines.append(f"    Daily spent:     ${daily_spent:.2f} / ${daily_limit:.2f}")

    remaining = daily_limit - daily_spent
    trade_cost = amount if is_market else (size * price if size and price else 0)
    if trade_cost > remaining:
        lines.append(f"    WARNING: Trade (${trade_cost:.2f}) exceeds remaining daily budget (${remaining:.2f})")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


def execute_limit_order(
    client: ClobClient,
    token_id: str,
    side: str,
    size: float,
    price: float,
) -> dict:
    """Create and post a limit order."""
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
    )
    signed_order = client.create_order(order_args)
    result = client.post_order(signed_order, orderType=OrderType.GTC)
    return result


def execute_market_order(
    client: ClobClient,
    token_id: str,
    side: str,
    amount: float,
) -> dict:
    """Create and post a market order."""
    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=amount,
        side=side,
    )
    signed_order = client.create_market_order(order_args)
    result = client.post_order(signed_order, orderType=OrderType.FOK)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Execute a live trade on Polymarket (requires confirmation)"
    )
    parser.add_argument("--token-id", required=True, help="CLOB token ID")
    parser.add_argument(
        "--side", required=True, choices=["BUY", "SELL"],
        help="Trade side: BUY or SELL"
    )
    parser.add_argument("--size", type=float, help="Number of shares (for limit orders)")
    parser.add_argument("--price", type=float, help="Limit price per share")
    parser.add_argument("--amount", type=float, help="USD amount (for market orders)")
    parser.add_argument("--market", action="store_true", help="Market order (FOK)")
    args = parser.parse_args()

    # Validate arguments
    if args.market:
        if not args.amount:
            print("ERROR: --amount required for market orders", file=sys.stderr)
            sys.exit(1)
    else:
        if not args.size or not args.price:
            print("ERROR: --size and --price required for limit orders", file=sys.stderr)
            sys.exit(1)

    # Check safety gates
    ok, msg = check_safety_gates()
    if not ok:
        print(f"SAFETY GATE FAILED:\n{msg}", file=sys.stderr)
        sys.exit(1)

    # Check position size limits
    max_size = get_max_size()
    trade_cost = args.amount if args.market else (args.size * args.price)

    if trade_cost > max_size:
        print(
            f"BLOCKED: Trade cost ${trade_cost:.2f} exceeds max size ${max_size:.2f}.\n"
            f"Increase POLYMARKET_MAX_SIZE if intentional.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check daily loss limit
    daily_limit = get_daily_loss_limit()
    daily_spent = get_daily_spending()
    if daily_spent + trade_cost > daily_limit:
        print(
            f"BLOCKED: Daily spending ${daily_spent:.2f} + this trade ${trade_cost:.2f} "
            f"= ${daily_spent + trade_cost:.2f} exceeds daily limit ${daily_limit:.2f}.\n"
            f"Increase POLYMARKET_DAILY_LOSS_LIMIT or wait until tomorrow.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Create authenticated client
    try:
        client = create_authenticated_client()
    except Exception as e:
        print(f"ERROR creating authenticated client: {e}", file=sys.stderr)
        print("Check POLYMARKET_PRIVATE_KEY and network connectivity.", file=sys.stderr)
        sys.exit(1)

    # Get order book context
    context = get_orderbook_context(client, args.token_id)

    # Display confirmation
    confirmation = display_trade_confirmation(
        side=args.side,
        token_id=args.token_id,
        size=args.size,
        amount=args.amount,
        price=args.price,
        is_market=args.market,
        context=context,
        max_size=max_size,
        daily_spent=daily_spent,
        daily_limit=daily_limit,
    )
    print(confirmation)

    # Ask for confirmation
    try:
        response = input("\n  Type 'yes' to execute this trade: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nTrade cancelled.")
        log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "CANCELLED",
            "reason": "User did not confirm",
            "token_id": args.token_id,
            "side": args.side,
            "size": args.size,
            "price": args.price,
            "amount": args.amount,
            "is_market": args.market,
        })
        sys.exit(0)

    if response != "yes":
        print("Trade cancelled. You must type exactly 'yes' to confirm.")
        log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "CANCELLED",
            "reason": f"User typed: {response!r}",
            "token_id": args.token_id,
            "side": args.side,
            "size": args.size,
            "price": args.price,
            "amount": args.amount,
            "is_market": args.market,
        })
        sys.exit(0)

    # Execute the trade
    print("\nExecuting trade...")
    try:
        if args.market:
            result = execute_market_order(client, args.token_id, args.side, args.amount)
        else:
            result = execute_limit_order(client, args.token_id, args.side, args.size, args.price)

        print(f"\nTrade submitted successfully!")
        print(json.dumps(result, indent=2, default=str))

        log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "EXECUTED",
            "token_id": args.token_id,
            "side": args.side,
            "size": args.size,
            "price": args.price,
            "amount": args.amount,
            "is_market": args.market,
            "cost_usd": trade_cost,
            "result": result,
        })

    except Exception as e:
        print(f"\nTrade FAILED: {e}", file=sys.stderr)
        log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "FAILED",
            "error": str(e),
            "token_id": args.token_id,
            "side": args.side,
            "size": args.size,
            "price": args.price,
            "amount": args.amount,
            "is_market": args.market,
            "cost_usd": trade_cost,
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
