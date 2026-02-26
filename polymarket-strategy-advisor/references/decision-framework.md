# Decision Framework for Prediction Market Trading

A complete, rule-based decision tree for entries, exits, position sizing,
and risk management. Follow these rules mechanically -- discretionary
overrides are the primary source of losses for prediction market traders.

## Entry Decision Tree

```
START: Market candidate identified
  |
  v
[1] Volume > $10K/24h? ----NO----> SKIP (illiquid)
  |YES
  v
[2] Spread < 10%? ----NO----> SKIP (too expensive to enter/exit)
  |YES
  v
[3] End date > 24h away? ----NO----> SKIP (near-resolution, efficiently priced)
  |YES
  v
[4] Accepting orders? ----NO----> SKIP (book closed)
  |YES
  v
[5] Can you classify the edge? ----NO----> SKIP (no edge = no trade)
  |YES
  v
[6] Edge > 5% (after fees)? ----NO----> SKIP (insufficient edge)
  |YES
  v
[7] Kelly size > 0? ----NO----> SKIP (negative EV by your own model)
  |YES
  v
[8] Risk rules pass? ----NO----> SKIP (risk limit reached)
  |YES
  v
TRADE
```

Every "SKIP" must be logged with the specific reason. This creates a record
for later review of whether your filters are too tight or too loose.

## Position Sizing Rules

### Kelly Criterion (Half-Kelly)

The Kelly criterion calculates the mathematically optimal bet size to
maximize long-term growth:

```
Full Kelly fraction = (p * b - q) / b

Where:
  p = your estimated probability of winning
  q = 1 - p (probability of losing)
  b = odds received (payout / risk)

For binary prediction markets:
  b = (1 - entry_price) / entry_price  (for YES bets)
  b = entry_price / (1 - entry_price)  (for NO bets)

Half-Kelly = Full Kelly / 2
Position size = portfolio_value * Half-Kelly
```

Why half-Kelly: Full Kelly is optimal only if your probability estimates
are perfectly calibrated. They are not. Half-Kelly sacrifices ~25% of
growth rate but reduces variance by 50% and reduces probability of ruin
dramatically.

### Hard Position Size Caps

These caps override Kelly regardless of what the formula says:

| Condition | Maximum Position Size |
|-----------|----------------------|
| Default | 10% of portfolio |
| Confidence < 0.7 | 5% of portfolio |
| News-driven edge | 2% of portfolio |
| First trade with a new strategy | 1% of portfolio |
| Arbitrage (both sides hedged) | 20% of portfolio |

### Minimum Position Size

Do not trade if the position would be less than $10 USDC. Transaction costs
and monitoring overhead make tiny positions not worth the effort.

## Exit Decision Tree

### Profit Exit (Target Hit)

```
For each edge type, the default target is:

  Arbitrage:      Hold to resolution (guaranteed profit)
  Momentum:       Exit at 80% of estimated move
  Mean Reversion: Exit when price returns to mean
  News-Driven:    Exit within 15 minutes or when market converges
```

When target is hit, exit the full position. Do not get greedy by moving
the target. The original analysis determined the edge -- respect it.

### Loss Exit (Stop Loss)

```
Default stop loss = entry_price - (edge / 2)

Example:
  Entry: 0.45 YES
  Estimated fair value: 0.55
  Edge: 0.10
  Stop loss: 0.45 - 0.05 = 0.40
```

When stop is hit, exit immediately. No exceptions. The most expensive words
in trading are "it will come back."

### Time-Based Exit

| Edge Type | Maximum Hold Time |
|-----------|-------------------|
| Arbitrage | Until resolution |
| Momentum | 48 hours |
| Mean Reversion | 24 hours |
| News-Driven | 15 minutes |

If the position has not hit target or stop within the time limit, exit at
market. The edge has either been captured or has decayed.

### Forced Exit Conditions

Exit ALL positions immediately if:
- Total portfolio drawdown exceeds 20%
- Daily loss exceeds 5%
- Three consecutive stop losses hit
- Market structure changes (API down, unusual activity, flash crash)

## Risk Budget

### Per-Trade Limits

- Maximum risk per trade: 2% of portfolio (defined as position_size *
  distance_to_stop / portfolio_value)
- Maximum number of open positions: 5
- Maximum correlated exposure: count correlated positions as ONE position

### Daily Limits

- Maximum daily loss: 5% of portfolio
- Maximum number of new trades per day: 10
- When daily loss limit is hit, all new entries are blocked until the next
  calendar day

### Weekly Limits

- Maximum weekly loss: 10% of portfolio
- When weekly loss limit is hit, all new entries are blocked until the next
  Monday

### Drawdown Limits

- At 10% drawdown from peak: reduce all position sizes by 50%
- At 15% drawdown from peak: reduce all position sizes by 75%, no new
  momentum or news trades
- At 20% drawdown from peak: close all positions, stop trading, full
  strategy review required

## Correlation Rules

Two positions are considered correlated if:
- They reference the same underlying event (e.g., "Will X win?" and
  "What will X's margin be?")
- They are in the same event group on Polymarket
- One outcome logically implies the other

Correlated positions count as a single position for concentration limits.
The combined size of correlated positions must not exceed the single-trade
size cap.

## When to STOP Trading Entirely

1. **Mechanical stop**: Any drawdown limit triggered (see above)
2. **Strategy failure**: Win rate drops below 40% over 20+ trades
3. **Model broken**: Three consecutive trades where the market moved
   opposite to your prediction by > 10 percentage points
4. **External factors**: Major regulatory news, platform issues, API
   instability
5. **Emotional state**: Feeling the need to "make it back", trading out
   of boredom, or ignoring your own rules

When stopped, conduct a full review:
- Were entries following the methodology?
- Were stop losses being respected?
- Was position sizing within limits?
- Has the market regime changed (volatility, liquidity)?

Resume only after identifying the issue and implementing a fix. If no
issue is found, reduce position sizes by 50% for the next 20 trades.

## Portfolio Review Schedule

### Daily (scripts/daily_review.py)

- Calculate P&L for all positions closed today
- Check open positions against current risk limits
- Verify no drawdown limits are being approached
- Review any skipped trades -- was the skip correct?

### Weekly

- Win rate by strategy type
- Average edge captured vs predicted
- Largest winner and largest loser -- were rules followed?
- Correlation exposure review
- Adjust parameters if evidence supports it (minimum 50 trades)

### Monthly

- Full strategy performance comparison
- Retire strategies with < 50% win rate over 100+ trades
- Evaluate new strategy candidates via paper trading
- Recalibrate Kelly inputs based on actual win rates
