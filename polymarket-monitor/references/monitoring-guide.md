# Polymarket Monitoring Guide

## Recommended Thresholds by Market Type

Different market categories have different volatility profiles. Use these thresholds as starting points:

| Market Type | Alert Threshold | Polling Interval | Notes |
|-------------|----------------|------------------|-------|
| Politics (major) | 3-5% | 60s | Slow-moving, big moves are meaningful |
| Politics (minor) | 5-10% | 120s | Less liquid, wider natural swings |
| Crypto (daily) | 5-10% | 30s | Moderate volatility |
| Crypto (5-min/15-min) | 10-20% | 10s | Very volatile, expect frequent alerts at low thresholds |
| Sports (pre-game) | 5-10% | 60s | Moves on news/lineups |
| Sports (live) | 15-25% | 15s | Rapid swings during games |
| Weather | 10-15% | 300s | Slow-moving, updates tied to forecasts |
| Entertainment | 10-15% | 120s | Event-driven spikes |

## Monitoring Strategies

### 1. Breakout Detection

Monitor markets trading in a narrow range. When price breaks out of the range, it often signals new information entering the market.

```bash
# Low threshold to catch early breakouts in high-volume politics market
python polymarket-monitor/scripts/monitor_prices.py \
  --token-id "<TOKEN>" --interval 30 --threshold 3.0
```

### 2. Multi-Market Correlation

Monitor related markets simultaneously. If one moves but others don't, the lagging markets may present opportunities.

```bash
# Watch all outcomes in a multi-outcome market
python polymarket-monitor/scripts/monitor_prices.py \
  --token-id "<YES_TOKEN>" --token-id "<NO_TOKEN>" \
  --interval 30 --threshold 5.0
```

### 3. Spread Monitoring

Use `watch_market.py` to track spread widening/narrowing. Widening spreads can signal uncertainty or liquidity withdrawal before a move.

```bash
python polymarket-monitor/scripts/watch_market.py \
  --token-id "<TOKEN>" --interval 15 --max-polls 20
```

### 4. Volume-Triggered Monitoring

First use the scanner to find high-volume markets, then set up monitoring:

```bash
# Step 1: Find active high-volume markets
python polymarket-scanner/scripts/scan_markets.py --min-volume 500000 --limit 5

# Step 2: Monitor the top market's token
python polymarket-monitor/scripts/monitor_prices.py \
  --token-id "<TOKEN_FROM_STEP_1>" --interval 30 --threshold 5.0
```

## Understanding Alerts

### Price Alert Fields

- **current_price**: The latest midpoint price (0 to 1)
- **baseline_price**: The reference price being compared against
- **change_pct**: Percentage change from baseline (positive = price increased)
- **direction**: "up" or "down"
- **spread**: Current bid-ask spread (wider = less liquid)

### What Triggers Are Meaningful?

| Signal | Interpretation |
|--------|---------------|
| Large move + narrow spread | Strong conviction move, likely real information |
| Large move + wide spread | Could be liquidity-driven, may revert |
| Small steady moves (same direction) | Trend forming, consider momentum |
| Alternating up/down alerts | Noise or market-maker rebalancing |

## Baseline Window

The `--baseline-window` parameter controls how the baseline price is calculated:

- **Window 1** (default): Compares to the previous poll. Catches fast moves but generates more noise.
- **Window 5**: Averages the last 5 polls. Smoother, fewer false alerts, but slower to detect gradual trends.
- **Window 10+**: Only triggers on sustained moves. Good for long-duration monitoring.

## Rate Limit Considerations

- Minimum polling interval is enforced at 5 seconds
- Each poll makes 1-2 API calls per token (midpoint + spread)
- `watch_market.py` makes 3-4 calls per poll (midpoint, spread, last trade, order book)
- For monitoring many tokens (10+), use intervals of 30s+ to avoid excessive API load
- The CLOB API is generous for read-only use but be respectful of shared resources

## Non-Interactive Use

For agent automation, use `--max-polls` to bound monitoring runs:

```bash
# Take 10 snapshots then stop (good for periodic checks)
python polymarket-monitor/scripts/watch_market.py \
  --token-id "<TOKEN>" --interval 15 --max-polls 10

# Monitor for ~5 minutes then stop
python polymarket-monitor/scripts/monitor_prices.py \
  --token-id "<TOKEN>" --interval 30 --threshold 5.0 --max-polls 10
```

Alerts go to stdout (JSON, one per line). Status messages go to stderr. Pipe stdout to capture only actionable alerts.
