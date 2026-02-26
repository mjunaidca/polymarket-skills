#!/usr/bin/env python3
"""
Polymarket Live Trading — Wallet Setup & Verification

Creates a burner wallet OR verifies an existing configuration.
Run this BEFORE your first live trade.

Usage:
    python setup_wallet.py --create          # Generate new burner wallet
    python setup_wallet.py --verify          # Check existing env vars
    python setup_wallet.py --check-balance   # Verify on-chain USDC balance
"""

import argparse
import json
import os
import sys
import stat
from pathlib import Path

# Polygon USDC contract
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_RPC = "https://polygon-rpc.com"


def create_wallet():
    """Generate a new burner wallet and output setup instructions."""
    try:
        from eth_account import Account
    except ImportError:
        print("ERROR: eth_account not installed.")
        print("Install with: pip install eth-account")
        print("")
        print("Alternative: use 'cast wallet new' (Foundry) or MetaMask.")
        sys.exit(1)

    acct = Account.create()
    address = acct.address
    private_key = acct.key.hex()

    print("=" * 60)
    print("  NEW BURNER WALLET CREATED")
    print("=" * 60)
    print(f"  Address:     {address}")
    print(f"  Private Key: {private_key}")
    print("=" * 60)
    print("")
    print("SAVE THE PRIVATE KEY NOW — it cannot be recovered.")
    print("")
    print("Next steps:")
    print(f"  1. Fund {address} with USDC on Polygon")
    print("     - Start with $25 or less (First Time tier)")
    print("     - Send ~0.1 MATIC for gas fees")
    print("  2. Set up your .env file:")
    print("")

    env_path = Path(__file__).parent.parent / ".env"
    example_path = Path(__file__).parent.parent / ".env.example"

    if not env_path.exists() and example_path.exists():
        print(f"     cp {example_path} {env_path}")
        print(f"     chmod 600 {env_path}")
        print(f"     # Edit {env_path} and paste your private key")
    else:
        print(f"     export POLYMARKET_PRIVATE_KEY={private_key}")
        print("     export POLYMARKET_CONFIRM=true")
        print("     export POLYMARKET_MAX_SIZE=5")
        print("     export POLYMARKET_DAILY_LOSS_LIMIT=10")

    print("")
    print("  3. Verify setup:")
    print("     python setup_wallet.py --verify")
    print("")
    print("SECURITY REMINDERS:")
    print("  - This is a BURNER wallet — only fund what you can lose")
    print("  - NEVER use your main wallet's private key")
    print("  - NEVER commit .env to git")
    print("  - NEVER paste the private key into chat")


def verify_config():
    """Check that all required env vars are set and valid."""
    print("=" * 60)
    print("  LIVE TRADING CONFIGURATION CHECK")
    print("=" * 60)
    print("")

    checks = []

    # Check POLYMARKET_PRIVATE_KEY
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        checks.append(("POLYMARKET_PRIVATE_KEY", "MISSING", "Not set"))
    elif not pk.startswith("0x") or len(pk) != 66:
        checks.append(("POLYMARKET_PRIVATE_KEY", "INVALID", "Must be 0x + 64 hex chars"))
    else:
        masked = pk[:6] + "..." + pk[-4:]
        checks.append(("POLYMARKET_PRIVATE_KEY", "OK", f"Set ({masked})"))

    # Check POLYMARKET_CONFIRM
    confirm = os.environ.get("POLYMARKET_CONFIRM", "")
    if confirm != "true":
        checks.append(("POLYMARKET_CONFIRM", "MISSING", f"Got '{confirm}', need 'true'"))
    else:
        checks.append(("POLYMARKET_CONFIRM", "OK", "Safety gate enabled"))

    # Check POLYMARKET_MAX_SIZE
    max_size = os.environ.get("POLYMARKET_MAX_SIZE", "")
    if not max_size:
        checks.append(("POLYMARKET_MAX_SIZE", "DEFAULT", "Not set, will use $10 default"))
    else:
        try:
            val = float(max_size)
            tier = "First time" if val <= 5 else "Learning" if val <= 10 else "Experienced" if val <= 50 else "Advanced"
            checks.append(("POLYMARKET_MAX_SIZE", "OK", f"${val:.0f} per trade ({tier} tier)"))
        except ValueError:
            checks.append(("POLYMARKET_MAX_SIZE", "INVALID", f"Got '{max_size}', need a number"))

    # Check POLYMARKET_DAILY_LOSS_LIMIT
    dll = os.environ.get("POLYMARKET_DAILY_LOSS_LIMIT", "")
    if not dll:
        checks.append(("POLYMARKET_DAILY_LOSS_LIMIT", "DEFAULT", "Not set, will use $50 default"))
    else:
        try:
            val = float(dll)
            checks.append(("POLYMARKET_DAILY_LOSS_LIMIT", "OK", f"${val:.0f} daily limit"))
        except ValueError:
            checks.append(("POLYMARKET_DAILY_LOSS_LIMIT", "INVALID", f"Got '{dll}', need a number"))

    # Check .env file permissions
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        mode = oct(stat.S_IMODE(env_path.stat().st_mode))
        if mode == "0o600":
            checks.append((".env permissions", "OK", f"{mode} (owner read/write only)"))
        else:
            checks.append((".env permissions", "WARN", f"{mode} — should be 0o600. Run: chmod 600 {env_path}"))
    else:
        checks.append((".env file", "INFO", "No .env file found (using shell env vars)"))

    # Check .gitignore
    gitignore_path = Path(__file__).parent.parent.parent / ".gitignore"
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        if ".env" in content:
            checks.append((".gitignore", "OK", ".env is gitignored"))
        else:
            checks.append((".gitignore", "WARN", ".env NOT in .gitignore — add it!"))

    # Print results
    all_ok = True
    for name, status, detail in checks:
        icon = {"OK": "+", "MISSING": "X", "INVALID": "X", "WARN": "!", "DEFAULT": "~", "INFO": "-"}
        print(f"  [{icon.get(status, '?')}] {name}: {detail}")
        if status in ("MISSING", "INVALID"):
            all_ok = False

    print("")
    if all_ok:
        print("  STATUS: READY for live trading")
        print("")
        print("  Before your first trade, also run:")
        print("    python setup_wallet.py --check-balance")
        print("    python check_positions.py --balance")
    else:
        print("  STATUS: NOT READY — fix the issues above")

    return all_ok


def check_balance():
    """Query on-chain USDC balance for the configured wallet."""
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk or not pk.startswith("0x") or len(pk) != 66:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set or invalid.")
        print("Run: python setup_wallet.py --verify")
        sys.exit(1)

    try:
        from eth_account import Account
        acct = Account.from_key(pk)
        address = acct.address
    except ImportError:
        print("ERROR: eth_account not installed. pip install eth-account")
        sys.exit(1)

    try:
        import requests
    except ImportError:
        print("ERROR: requests not installed. pip install requests")
        sys.exit(1)

    print(f"Checking balance for: {address}")
    print("")

    # Check MATIC balance
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1,
    }
    try:
        resp = requests.post(POLYGON_RPC, json=payload, timeout=10)
        result = resp.json().get("result", "0x0")
        matic_wei = int(result, 16)
        matic = matic_wei / 1e18
        matic_status = "OK" if matic >= 0.01 else "LOW — need ~0.1 MATIC for gas"
        print(f"  MATIC: {matic:.4f} ({matic_status})")
    except Exception as e:
        print(f"  MATIC: ERROR fetching — {e}")

    # Check USDC balance (ERC20 balanceOf)
    # balanceOf(address) selector = 0x70a08231
    padded_addr = address[2:].lower().zfill(64)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": USDC_CONTRACT, "data": f"0x70a08231{padded_addr}"},
            "latest",
        ],
        "id": 2,
    }
    try:
        resp = requests.post(POLYGON_RPC, json=payload, timeout=10)
        result = resp.json().get("result", "0x0")
        usdc_raw = int(result, 16)
        usdc = usdc_raw / 1e6  # USDC has 6 decimals
        tier = "First time" if usdc <= 25 else "Learning" if usdc <= 100 else "Experienced" if usdc <= 500 else "Advanced"
        print(f"  USDC:  ${usdc:.2f} ({tier} tier)")
    except Exception as e:
        print(f"  USDC:  ERROR fetching — {e}")

    print("")
    print("If balance is zero, fund the wallet:")
    print(f"  Send USDC (Polygon) to: {address}")
    print("  Send ~0.1 MATIC for gas fees")


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket live trading wallet setup and verification"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--create", action="store_true", help="Generate a new burner wallet"
    )
    group.add_argument(
        "--verify", action="store_true", help="Verify existing env var configuration"
    )
    group.add_argument(
        "--check-balance",
        action="store_true",
        help="Check on-chain USDC and MATIC balance",
    )
    args = parser.parse_args()

    if args.create:
        create_wallet()
    elif args.verify:
        verify_config()
    elif args.check_balance:
        check_balance()


if __name__ == "__main__":
    main()
