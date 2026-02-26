# Pre-Flight Checklist for Live Trading

Complete ALL items before executing your first live trade.

## Wallet Setup

- [ ] Created a dedicated burner wallet (NOT your main wallet)
- [ ] Funded with an amount you can afford to lose entirely
- [ ] Verified wallet has USDC on Polygon network
- [ ] Verified wallet has small MATIC balance for gas
- [ ] Private key stored in environment variable only (not in files or code)

## Environment Configuration

- [ ] `POLYMARKET_PRIVATE_KEY` is set to burner wallet key
- [ ] `POLYMARKET_CONFIRM=true` is set (required safety gate)
- [ ] `POLYMARKET_MAX_SIZE` is set to your per-trade limit (default: $10)
- [ ] `POLYMARKET_DAILY_LOSS_LIMIT` is set (default: $50)
- [ ] Verified configuration with `check_positions.py --balance`

## Analysis Completed

- [ ] Identified specific market and opportunity using polymarket-analyzer
- [ ] Reviewed order book depth with `analyze_orderbook.py`
- [ ] Confirmed sufficient liquidity at target price
- [ ] Understood the market's resolution criteria
- [ ] Checked market end date (not expiring imminently)

## Paper Trading Validation

- [ ] Tested the same strategy in paper trading mode first
- [ ] Paper trading results reviewed and acceptable
- [ ] Understand expected win rate and risk/reward ratio
- [ ] Strategy has shown positive expectancy in paper trades

## Risk Management

- [ ] Set maximum position size per trade
- [ ] Set daily loss limit
- [ ] Decided on exit strategy (when to take profit, when to cut loss)
- [ ] Understand that prediction markets can go to 0 or 1
- [ ] Accepted that all funds in burner wallet could be lost
- [ ] Not trading with money needed for bills, rent, or essentials

## Execution Plan

- [ ] Know the exact token ID for the trade
- [ ] Know the side (BUY or SELL)
- [ ] Know the size (number of shares or dollar amount)
- [ ] Know the price (limit) or willing to accept market price
- [ ] Reviewed current bid-ask spread
- [ ] Will carefully review the confirmation prompt before approving

## After the Trade

- [ ] Verified trade executed at expected price (check trades.log)
- [ ] Set a reminder to check position before market resolution
- [ ] Know how to cancel open limit orders if needed
- [ ] Plan for monitoring: will check at least daily

## Emergency Procedures

If something goes wrong:
1. Unset `POLYMARKET_CONFIRM` to prevent any further trades
2. Use `check_positions.py --orders` to see open orders
3. Cancel all open orders if needed
4. Transfer remaining funds out of burner wallet if compromised
5. Create a new burner wallet if the old one may be exposed
