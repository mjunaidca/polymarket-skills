# Polymarket Market Types

## Categories

### Politics
- Presidential elections, congressional races, cabinet appointments, policy decisions
- Typically high volume ($1M-$100M+), deep liquidity
- Long-dated (weeks to years), gradual price movement
- Most popular category by total volume

### Crypto
- Price targets (BTC above $X by date), ETF approvals, protocol events
- Sub-types: 5-minute, 15-minute, hourly, daily, weekly resolution
- Short-dated crypto markets have high turnover but thin books
- Daily/weekly crypto markets have moderate liquidity

### Sports
- Game outcomes, player props, tournament winners
- High volume around major events (Super Bowl, World Cup, March Madness)
- Maker/taker fees: 0 bps for most sports markets (fee-free)
- Time-sensitive: liquidity concentrates near game time

### Weather
- Temperature records, hurricane landfalls, seasonal forecasts
- Lower volume but potentially mispriced (fewer sophisticated traders)
- Asymmetric payoff opportunities when consensus is wrong
- Resolution tied to official weather service data

### Entertainment / Pop Culture
- Award shows, TV ratings, celebrity events
- Moderate volume, spiky around events
- Often mispriced due to sentiment bias

### Science / Technology
- AI milestones, space launches, drug approvals
- Long-dated, lower liquidity
- Pricing reflects expert consensus; edges come from domain knowledge

### Economics / Finance
- Fed rate decisions, inflation data, GDP
- High volume around FOMC meetings and data releases
- Prices move sharply on news; fast execution matters

## Fee Structure

Polymarket uses a maker/taker fee model on the CLOB:

| Fee Type | Rate |
|----------|------|
| Maker fee | 0 bps (free) |
| Taker fee | Varies by market (0-200 bps) |

Some markets (notably sports) are fee-free for both makers and takers as promotional incentives. Check the `maker_base_fee` and `taker_base_fee` fields in the CLOB market data.

## Market Mechanics

### Binary Markets
- Two outcomes: YES and NO
- Prices range from $0.00 to $1.00
- YES + NO should approximately equal $1.00 (minus spread)
- When YES + NO > $1.00, arbitrage opportunity exists (sell both)
- When YES + NO < $1.00, arbitrage opportunity exists (buy both)

### Multi-Outcome Markets
- Multiple mutually exclusive outcomes (e.g., "Who will win the election?")
- Each outcome has its own token ID and price
- All outcome prices should sum to approximately $1.00
- Often use `negRisk` (negative risk) framework

### Resolution
- Markets resolve to $1.00 for the winning outcome, $0.00 for losers
- Resolution source specified in market description
- UMA oracle used for dispute resolution
- `umaBond` and `umaReward` fields indicate dispute economics

## Volume Characteristics

| Category | Typical 24h Volume | Liquidity Depth | Spread |
|----------|-------------------|-----------------|--------|
| Politics (major) | $1M-$50M+ | $500K-$5M | 0.1-1% |
| Crypto (daily) | $100K-$5M | $50K-$500K | 0.5-2% |
| Crypto (5-min) | $10K-$100K | $5K-$50K | 1-5% |
| Sports (major) | $500K-$10M | $100K-$1M | 0.5-2% |
| Weather | $10K-$500K | $5K-$100K | 2-10% |
| Entertainment | $50K-$1M | $10K-$200K | 1-5% |

## Tick Sizes

Markets have minimum price increments (`orderPriceMinTickSize`):
- Most markets: 0.01 ($0.01 increments)
- High-liquidity markets: 0.001 ($0.001 increments)
- Minimum order size: typically 5-15 USDC (`orderMinSize`)

## Identifying Opportunities

### By Category
- **Highest volume:** Politics > Crypto > Sports
- **Most mispriced:** Weather > Entertainment (fewer sophisticated traders)
- **Fastest resolution:** Crypto 5-min > Sports > Politics

### By Market Structure
- Wide spreads (>2%) suggest market-making opportunities
- YES+NO deviating from $1.00 signals arbitrage
- High volume with thin books signals momentum trading potential
- New markets (<24h old) often have inefficient pricing
