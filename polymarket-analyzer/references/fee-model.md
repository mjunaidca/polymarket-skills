# Polymarket Fee Model

## Overview

Most Polymarket markets are **fee-free**. Dynamic taker fees apply only to
short-duration crypto markets (5-minute and 15-minute expiry).

## Fee-Free Markets

The vast majority of markets on Polymarket -- political, sports, entertainment,
weather, and long-duration crypto markets -- charge **zero fees** for both makers
and takers. This makes arbitrage significantly more viable than on traditional
exchanges.

## Dynamic Taker Fees (Crypto Short-Duration Only)

For 5-minute and 15-minute crypto prediction markets, a dynamic taker fee applies:

```
feeQuote = baseRate * min(price, 1 - price) * size
```

Where:
- `baseRate` is set per market (typically 0.063 or 6.3%)
- `price` is the execution price (0 to 1)
- `size` is the number of shares

### Effective Fee Rate by Price

| Price | min(p, 1-p) | Effective Rate (baseRate=0.063) |
|-------|-------------|-------------------------------|
| 0.05  | 0.05        | 0.315% (0.063 * 0.05)        |
| 0.10  | 0.10        | 0.630%                        |
| 0.20  | 0.20        | 1.260%                        |
| 0.30  | 0.30        | 1.890%                        |
| 0.40  | 0.40        | 2.520%                        |
| 0.50  | 0.50        | 3.150% (maximum)              |
| 0.60  | 0.40        | 2.520%                        |
| 0.70  | 0.30        | 1.890%                        |
| 0.80  | 0.20        | 1.260%                        |
| 0.90  | 0.10        | 0.630%                        |
| 0.95  | 0.05        | 0.315%                        |

The fee is **parabolic**, peaking at p=0.50 and dropping sharply near the extremes.
This was explicitly designed to kill latency arbitrage on these fast markets.

### Fee Calculator

```python
def calculate_fee(price: float, size: float, base_rate: float = 0.063) -> dict:
    """Calculate dynamic taker fee for crypto short-duration markets."""
    fee_rate = base_rate * min(price, 1 - price)
    fee_amount = fee_rate * size
    cost_basis = price * size
    total_cost = cost_basis + fee_amount
    effective_rate = fee_amount / cost_basis if cost_basis > 0 else 0

    return {
        "fee_rate": fee_rate,
        "fee_amount": fee_amount,
        "cost_basis": cost_basis,
        "total_cost": total_cost,
        "effective_rate_pct": effective_rate * 100,
    }
```

### Breakeven Analysis for Arbitrage

For an arbitrage trade buying both YES and NO:

```python
def arbitrage_breakeven(yes_price, no_price, base_rate=0.063):
    """Calculate if arb is profitable after fees on fee-bearing markets."""
    raw_sum = yes_price + no_price
    raw_edge = 1.0 - raw_sum  # Positive = underpriced

    yes_fee = base_rate * min(yes_price, 1 - yes_price)
    no_fee = base_rate * min(no_price, 1 - no_price)
    total_fee_rate = yes_fee + no_fee

    net_profit_per_share = raw_edge - total_fee_rate
    return {
        "raw_edge": raw_edge,
        "total_fee_rate": total_fee_rate,
        "net_profit_per_share": net_profit_per_share,
        "profitable": net_profit_per_share > 0,
    }
```

## Maker Rebates

Post-only limit orders (introduced January 2026) receive maker rebates on
qualifying markets. This creates a structural advantage for market-making
strategies that provide liquidity.

## Practical Implications

1. **Fee-free markets**: Arbitrage edges as small as $0.01 are worth capturing
2. **Fee-bearing markets**: Need at least 3-6% raw edge at mid-prices to break even
3. **Extreme prices** (< 0.10 or > 0.90): Fees are minimal even on fee-bearing markets
4. **Market making**: Maker rebates make spread-capture profitable on thin books
