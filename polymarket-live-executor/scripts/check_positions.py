#!/usr/bin/env python3
"""Check Polymarket positions, balances, and trade history.

Requires environment variable:
  POLYMARKET_PRIVATE_KEY - Wallet private key for authenticated access

Displays: wallet address, USDC balance, open orders, recent trades.
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
    BalanceAllowanceParams,
    OpenOrderParams,
    TradeParams,
)


CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
LOG_FILE = Path.home() / ".polymarket-live" / "trades.log"


def create_authenticated_client() -> ClobClient:
    """Create an L2-authenticated ClobClient."""
    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not key:
        print(
            "POLYMARKET_PRIVATE_KEY not set.\n"
            "Set it to your wallet private key to view positions.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=key)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


def show_balance(client: ClobClient):
    """Display USDC balance and allowance."""
    address = client.get_address()
    print(f"Wallet Address: {address}")
    print()

    try:
        params = BalanceAllowanceParams(asset_type="COLLATERAL")
        result = client.get_balance_allowance(params)
        print("USDC Balance & Allowance:")
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(f"Error fetching balance: {e}", file=sys.stderr)


def show_orders(client: ClobClient):
    """Display open orders."""
    print("Open Orders:")
    print("-" * 70)

    try:
        orders = client.get_orders()
        if not orders:
            print("  No open orders.")
            return

        for order in orders:
            print(json.dumps(order, indent=2, default=str))
            print()
    except Exception as e:
        print(f"Error fetching orders: {e}", file=sys.stderr)


def show_trades(client: ClobClient):
    """Display recent trade history."""
    print("Recent Trades:")
    print("-" * 70)

    try:
        trades = client.get_trades()
        if not trades:
            print("  No trades found.")
            return

        for trade in trades[:20]:  # Show last 20
            print(json.dumps(trade, indent=2, default=str))
            print()
    except Exception as e:
        print(f"Error fetching trades: {e}", file=sys.stderr)


def show_local_log():
    """Display local trade log."""
    print("Local Trade Log (~/.polymarket-live/trades.log):")
    print("-" * 70)

    if not LOG_FILE.exists():
        print("  No local trade log found.")
        return

    lines = LOG_FILE.read_text().splitlines()
    if not lines:
        print("  Trade log is empty.")
        return

    # Show last 20 entries
    for line in lines[-20:]:
        try:
            entry = json.loads(line)
            ts = entry.get("timestamp", "?")[:19]
            status = entry.get("status", "?")
            side = entry.get("side", "?")
            token = entry.get("token_id", "?")[:20]
            cost = entry.get("cost_usd", "?")
            print(f"  {ts}  {status:<10} {side:<5} ${cost}  {token}...")
        except json.JSONDecodeError:
            print(f"  {line[:80]}")

    # Show today's summary
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_total = 0.0
    daily_count = 0
    for line in lines:
        try:
            entry = json.loads(line)
            if entry.get("timestamp", "").startswith(today) and entry.get("status") == "EXECUTED":
                daily_total += float(entry.get("cost_usd", 0))
                daily_count += 1
        except (json.JSONDecodeError, ValueError):
            continue

    print()
    print(f"  Today ({today}): {daily_count} trades, ${daily_total:.2f} total spent")


def show_summary(client: ClobClient):
    """Display a comprehensive summary."""
    address = client.get_address()
    print(f"Polymarket Position Summary")
    print(f"Wallet: {address}")
    print("=" * 60)

    # Balance
    print()
    show_balance(client)

    # Open orders
    print()
    show_orders(client)

    # Local log summary
    print()
    show_local_log()

    # Safety status
    print()
    print("Safety Status:")
    confirm = os.environ.get("POLYMARKET_CONFIRM", "")
    max_size = os.environ.get("POLYMARKET_MAX_SIZE", "10")
    daily_limit = os.environ.get("POLYMARKET_DAILY_LOSS_LIMIT", "50")
    print(f"  POLYMARKET_CONFIRM:          {'ENABLED' if confirm == 'true' else 'DISABLED (trades blocked)'}")
    print(f"  POLYMARKET_MAX_SIZE:         ${max_size}")
    print(f"  POLYMARKET_DAILY_LOSS_LIMIT: ${daily_limit}")


def main():
    parser = argparse.ArgumentParser(
        description="Check Polymarket positions and balances"
    )
    parser.add_argument("--balance", action="store_true", help="Show USDC balance only")
    parser.add_argument("--orders", action="store_true", help="Show open orders only")
    parser.add_argument("--trades", action="store_true", help="Show trade history only")
    parser.add_argument("--log", action="store_true", help="Show local trade log only")
    args = parser.parse_args()

    # Local log doesn't need authentication
    if args.log:
        show_local_log()
        return

    client = create_authenticated_client()

    if args.balance:
        show_balance(client)
    elif args.orders:
        show_orders(client)
    elif args.trades:
        show_trades(client)
    else:
        show_summary(client)


if __name__ == "__main__":
    main()
