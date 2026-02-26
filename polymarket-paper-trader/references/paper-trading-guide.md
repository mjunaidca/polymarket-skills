# Paper Trading Guide

## Why Paper Trading?

Polymarket has **no official testnet**. There is no sandbox environment where you can practice with fake money against real market infrastructure. This paper trading engine fills that gap by:

1. Reading **real live prices** from the Polymarket CLOB API
2. Simulating fills against the **actual order book**
3. Tracking everything in a local SQLite database
4. Enforcing realistic risk management rules

You get the experience of trading with real market data, but zero financial risk.

## How the Simulation Works

### Market Orders (Recommended)

When you place a market order (no `--price` flag), the engine:

1. Fetches the live order book from `clob.polymarket.com/book`
2. Walks through price levels to simulate a realistic fill
3. For a BUY: consumes ask levels ascending (cheapest first)
4. For a SELL: consumes bid levels descending (most expensive first)
5. Calculates a weighted average fill price across all levels consumed
6. Applies the fee model

This means larger orders get **worse average prices** (price impact), just like in real trading.

### Limit Orders

When you specify a price with `--price`:
- The order fills entirely at that price
- No order book simulation is performed
- Use this to model "I would have gotten filled at this price"

### Fee Model

| Market Type | Fee Rate |
|-------------|----------|
| Most markets | 0% |
| Crypto 5-min | ~2% maker / 5% taker (dynamic) |
| Crypto 15-min | ~1% maker / 3% taker (dynamic) |

The default fee rate is 0%. Override with `--fee-rate 0.02` for crypto markets.

Polymarket recently removed fees on most markets. The engine defaults to fee-free to match current behavior, but the fee infrastructure remains for markets that charge fees.

## Limitations

This is a simulation. Key differences from real trading:

### What IS Simulated
- Live market prices (real order book data)
- Order book walking (price impact on large orders)
- Portfolio tracking (balance, positions, P&L)
- Risk management (position limits, drawdown)
- Fee deduction

### What is NOT Simulated
- **Slippage from order latency**: Real orders take time to reach the exchange. Prices can move between decision and execution.
- **Market impact**: Your real order would move the book. The simulation reads the book without affecting it.
- **Partial fills**: Limit orders always fill completely or not at all. Real limit orders may partially fill.
- **Order queue position**: Real limit orders wait in a queue. The simulation fills instantly.
- **Market resolution**: When a market resolves, positions should auto-close at $0 or $1. Currently the engine does not auto-detect resolution.
- **Funding/settlement**: Real Polymarket uses USDC on Polygon. The simulation uses abstract USD.

### Practical Impact

These limitations mean paper trading results tend to be **slightly optimistic**:
- No slippage = better fill prices than reality
- No market impact = can trade larger sizes than realistic
- No partial fills = more consistent execution

Rule of thumb: expect real results to be 10-20% worse than paper results.

## Workflow: From Paper to Live

### Phase 1: Paper Trading (This Skill)
1. Initialize portfolio: `--action init --balance 1000`
2. Trade using scanner + analyzer insights
3. Run for at least 2 weeks / 20+ trades
4. Generate performance report

### Phase 2: Validate Results
Review the portfolio report. Key thresholds before going live:
- Win rate > 55% over 20+ closed trades
- Sharpe ratio > 0.5
- Max drawdown < 15%
- Profit factor > 1.2

If you don't hit these benchmarks, refine your strategy and paper trade longer.

### Phase 3: Live Trading (polymarket-live-executor skill)
When ready, transition to the live executor:
- Start with 10-25% of the capital you paper traded with
- Use the same risk rules (or tighter)
- Compare live results to paper results weekly
- Scale up gradually if live matches paper within 20%

## Database Schema

Portfolio data is stored at `~/.polymarket-paper/portfolio.db`:

- **portfolios**: Account state (balance, peak value, risk config)
- **positions**: Open and closed positions with entry/exit prices
- **trades**: Full trade log with timestamps and reasoning
- **daily_snapshots**: End-of-day portfolio values for performance tracking

## Tips

- **Take daily snapshots**: Run `--action snapshot` daily for accurate Sharpe/Sortino calculations
- **Always include reasoning**: The `--reason` flag creates an audit trail for strategy improvement
- **Start with scanner**: Use the polymarket-scanner skill to find markets before trading
- **Check risk limits**: The engine will reject trades that violate risk rules. Don't bypass with `--force` unless you understand why
- **Use JSON output**: Add `--json` for programmatic integration with other skills
