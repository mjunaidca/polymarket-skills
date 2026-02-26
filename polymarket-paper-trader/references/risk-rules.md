# Risk Rules Reference

## Default Risk Parameters

These rules are enforced automatically on every trade. They can be overridden per-portfolio at initialization or bypassed with `--force` (not recommended).

### Position Sizing

| Parameter | Default | Key |
|-----------|---------|-----|
| Max single trade | 10% of portfolio value | `max_position_pct` |
| Max single market exposure | 20% of portfolio value | `max_single_market_pct` |
| Human approval required | Trades > 15% of portfolio | `human_approval_pct` |

**Rationale**: Prediction markets have binary outcomes. A 10% max position ensures no single wrong bet destroys the portfolio. The 20% market cap prevents over-concentration in correlated outcomes (e.g., multiple markets about the same event).

### Drawdown Controls

| Parameter | Default | Key |
|-----------|---------|-----|
| Max total drawdown | 30% from peak | `max_drawdown_pct` |
| Daily loss limit | 5% of starting balance | `daily_loss_limit_pct` |

**Behavior when triggered**:
- **Max drawdown**: ALL trading halted. No new positions allowed. Existing positions remain open.
- **Daily loss limit**: No new trades for the rest of the day (UTC). Resets at midnight UTC.

### Position Limits

| Parameter | Default | Key |
|-----------|---------|-----|
| Max concurrent positions | 5 | `max_concurrent_positions` |

Adding to an existing position does not count as a new position.

## Custom Risk Configuration

Pass a custom config when initializing a portfolio:

```python
from paper_engine import init_portfolio

init_portfolio(
    starting_balance=5000,
    risk_config={
        "max_position_pct": 0.05,          # More conservative: 5%
        "max_drawdown_pct": 0.20,          # Tighter drawdown: 20%
        "max_concurrent_positions": 10,     # More diversified
        "daily_loss_limit_pct": 0.03,      # Tighter daily limit: 3%
        "max_single_market_pct": 0.15,     # 15% per market
        "human_approval_pct": 0.10,        # Approve trades > 10%
    }
)
```

## Kelly Criterion Sizing

The `execute_paper.py` executor uses a half-Kelly sizing formula when no explicit size is given:

```
kelly_fraction = max(0, (2 * confidence - 1)) * 0.5
size = portfolio_value * min(kelly_fraction, 0.10)
```

This means:
- **50% confidence** = 0% of portfolio (break-even, no bet)
- **60% confidence** = 5% of portfolio
- **70% confidence** = 10% of portfolio (capped at max_position_pct)
- **80%+ confidence** = 10% of portfolio (hard cap)

Half-Kelly is used instead of full Kelly because:
1. Confidence estimates are noisy (model uncertainty)
2. Prediction market odds already embed crowd wisdom
3. Half-Kelly has 75% of the growth rate with far lower variance

## Risk Check Order

1. Balance check (always enforced, even with `--force`)
2. Position size vs portfolio
3. Drawdown check
4. Concurrent position limit
5. Single market concentration
6. Human approval threshold
7. Daily loss limit

## Emergency Override

The `--force` flag bypasses rules 2-6. Balance check (rule 1) cannot be bypassed. Daily loss limit is also checked but can be overridden.

Use `--force` only for:
- Testing and development
- Closing positions in distress
- When a human has explicitly approved the trade
