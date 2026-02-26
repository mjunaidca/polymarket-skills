# Security Guide for Live Polymarket Trading

## Rule #1: NEVER Use Your Main Wallet

Always create a dedicated **burner wallet** for bot trading. This wallet should:
- Be a fresh address with no connection to your primary holdings
- Hold only the amount you are willing to lose entirely
- Never be used for any other purpose

If your private key is compromised (through env var leaks, log exposure, or
prompt injection attacks), only the burner wallet funds are at risk.

## Creating a Burner Wallet

### Option A: Using Python

```python
from eth_account import Account
acct = Account.create()
print(f"Address: {acct.address}")
print(f"Private Key: {acct.key.hex()}")
# SAVE THESE SECURELY. Fund address with USDC on Polygon.
```

### Option B: Using MetaMask

1. Create a new MetaMask profile (not just a new account in existing profile)
2. Create a new wallet
3. Export the private key (Settings > Security > Export Private Key)
4. Fund with USDC on Polygon network

### Option C: Using cast (Foundry)

```bash
cast wallet new
# Save the address and private key
```

## Funding the Burner Wallet

1. Bridge USDC to Polygon (use official Polygon bridge or a reputable DEX)
2. Send only your maximum acceptable loss to the burner address
3. Keep some MATIC for gas fees (~0.1 MATIC is usually sufficient)

## Setting Up L2 Authentication

The Polymarket CLOB API uses three-tier authentication:

- **L0**: No auth. Read-only market data.
- **L1**: Wallet signature. Can create/sign orders.
- **L2**: API key + secret + passphrase. Can post orders, manage positions.

To set up L2:

```python
from py_clob_client.client import ClobClient

# Initialize with L1 (private key)
client = ClobClient(
    "https://clob.polymarket.com",
    chain_id=137,  # Polygon mainnet
    key="0xYOUR_PRIVATE_KEY"
)

# Create API credentials (L2)
creds = client.create_or_derive_api_creds()
print(f"API Key: {creds.api_key}")
print(f"API Secret: {creds.api_secret}")
print(f"API Passphrase: {creds.api_passphrase}")
```

Store these credentials securely. The execute_live.py script handles this
automatically when POLYMARKET_PRIVATE_KEY is set.

## Private Key Handling Best Practices

### DO:
- Store the private key in an environment variable (`POLYMARKET_PRIVATE_KEY`)
- Use a `.env` file with strict permissions (`chmod 600 .env`)
- Unset the variable when not actively trading (`unset POLYMARKET_PRIVATE_KEY`)
- Rotate burner wallets periodically (create new wallet, transfer remaining funds)

### DO NOT:
- Hardcode private keys in any script or config file
- Commit `.env` files to version control
- Share private keys in chat, logs, or error reports
- Use the same key for testing and production
- Give the LLM/agent direct access to your private key in conversation
- Store keys in world-readable files

### Gitignore Template

Add to your `.gitignore`:
```
.env
.env.*
*.key
polymarket-live/
~/.polymarket-live/
```

## Prompt Injection Defense

When using AI agents for trading, be aware of prompt injection risks:

1. **Never paste untrusted content into agent context** when live trading is enabled
2. **Market descriptions could contain malicious instructions** -- the agent should
   never execute trades based on text found in market descriptions
3. **The POLYMARKET_CONFIRM=true env var** acts as a safety gate -- without it,
   no trade can execute regardless of what the agent is told to do
4. **Review every trade confirmation** before approving -- the agent must show you
   the exact parameters before execution

## Maximum Funding Recommendations

| Experience Level | Max Wallet Fund | Max Per Trade | Daily Loss Limit |
|------------------|-----------------|---------------|------------------|
| First time       | $25             | $5            | $10              |
| Learning         | $100            | $10           | $25              |
| Experienced      | $500            | $50           | $100             |
| Advanced         | $2,000+         | $200          | $500             |

Start small. You can always add more funds later.
