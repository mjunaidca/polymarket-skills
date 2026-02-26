# Polymarket AI Trading Skills

Composable [Agent Skills](https://agentskills.io/specification) for Polymarket prediction market trading. Paper-trading-first, security-audited, works with Claude Code, OpenClaw, NanoClaw, Codex, Cursor, and any SKILL.md-compatible agent.

## Skills

| Skill | What It Does | Auth | Risk |
|-------|-------------|------|------|
| **polymarket-scanner** | Browse, search, and explore live markets | None | Zero |
| **polymarket-analyzer** | Detect edges: arbitrage, momentum, correlation analysis | None | Zero |
| **polymarket-monitor** | Price alerts and position monitoring | None | Zero |
| **polymarket-paper-trader** | Simulate trades against live prices, portfolio health checks | None | Zero |
| **polymarket-strategy-advisor** | Trading methodology, recommendations, backtesting | None | Low |
| **polymarket-live-executor** | Execute real trades (wallet + explicit human opt-in) | L2 Wallet | Medium |

## Install

### One Command (All Skills)
```bash
npx skills add mjunaidca/polymarket-skills
```

### Individual Skills
```bash
npx skills add mjunaidca/polymarket-skills --skill polymarket-scanner
npx skills add mjunaidca/polymarket-skills --skill polymarket-analyzer
npx skills add mjunaidca/polymarket-skills --skill polymarket-paper-trader
# etc.
```

### Manual
Copy any skill folder to `~/.claude/skills/` (Claude Code) or `~/.agents/skills/` (other agents).

### Dependencies
```bash
pip install py-clob-client requests
```

## Quick Start

Once installed, talk to your agent naturally:

- *"Scan Polymarket for interesting markets"* -- triggers polymarket-scanner
- *"Find trading opportunities"* -- triggers polymarket-analyzer
- *"Set up a paper trading portfolio with $1000"* -- triggers polymarket-paper-trader
- *"What should I trade?"* -- triggers polymarket-strategy-advisor
- *"Check my portfolio health"* -- triggers polymarket-paper-trader (health_check)
- *"Am I ready to go live?"* -- triggers polymarket-strategy-advisor (backtest --live-check)
- *"Check correlation risk"* -- triggers polymarket-analyzer (correlation_tracker)

### Full Pipeline

```bash
source ~/.venv/bin/activate

# 1. Initialize portfolio
python polymarket-paper-trader/scripts/paper_engine.py --action init --balance 1000

# 2. Scan markets
python polymarket-scanner/scripts/scan_markets.py --limit 50 --min-volume 10000

# 3. Find edges
python polymarket-analyzer/scripts/find_edges.py --min-edge 0.02 --limit 500

# 4. Scan momentum
python polymarket-analyzer/scripts/momentum_scanner.py --min-volume 10000

# 5. Get trade recommendations
python polymarket-strategy-advisor/scripts/advisor.py --top 5 \
  --portfolio-db ~/.polymarket-paper/portfolio.db

# 6. Paper trade
python polymarket-paper-trader/scripts/paper_engine.py \
  --action buy --token TOKEN_ID --side YES --size 50 --reason "Momentum signal"

# 7. Check portfolio health (one command)
python polymarket-paper-trader/scripts/health_check.py

# 8. Check correlation risk
python polymarket-analyzer/scripts/correlation_tracker.py

# 9. Review performance
python polymarket-strategy-advisor/scripts/backtest.py

# 10. Daily review
python polymarket-strategy-advisor/scripts/daily_review.py --days 7
```

## Architecture

```
Scanner --> Analyzer --> Strategy Advisor --> Paper Trader --> Live Executor
(find)      (evaluate)    (recommend)          (simulate)       (execute)
```

### polymarket-scanner/ -- Market Data (Read-Only)

| Script | Purpose | Key Args |
|--------|---------|----------|
| `scan_markets.py` | Browse/search active markets from Gamma API | `--limit 100 --min-volume 10000 --category TEXT --search TEXT` |
| `get_orderbook.py` | Full order book for a specific token | `--token-id ID --depth 10` |
| `get_prices.py` | Current midpoint, spread, last trade | `--token-id ID (repeatable) --market-slug SLUG` |

### polymarket-analyzer/ -- Edge Detection

| Script | Purpose | Key Args |
|--------|---------|----------|
| `find_edges.py` | Arbitrage (YES+NO < $1), wide spread, overpriced detection | `--min-edge 0.02 --limit 500` |
| `momentum_scanner.py` | Volume surges, price momentum, liquidity changes | `--min-volume 10000 --limit 300` |
| `analyze_orderbook.py` | Depth analysis, bid-ask imbalance, liquidity concentration | `--token-id ID --depth 10` |
| `correlation_tracker.py` | Detects hidden correlated exposure in portfolio | `--threshold 0.15 --json` |

### polymarket-monitor/ -- Price Monitoring

| Script | Purpose | Key Args |
|--------|---------|----------|
| `monitor_prices.py` | Multi-token polling with threshold alerts (JSON) | `--token-id ID --interval 30 --threshold 5.0` |
| `watch_market.py` | Continuous single-market snapshots | `--token-id ID --interval 15` |

### polymarket-paper-trader/ -- Simulation Engine

| Script | Purpose | Key Args |
|--------|---------|----------|
| `paper_engine.py` | Core engine: init, buy, close, portfolio, trades | `--action init/buy/close/portfolio/trades` |
| `execute_paper.py` | Execute structured strategy recommendations | `--recommendation '{"token_id":...}' --dry-run` |
| `portfolio_report.py` | Full analytics: Sharpe, Sortino, drawdown | `--json` |
| `health_check.py` | One-command session start: live prices, stops, drawdown, risk | `--json` (exit codes: 0=GREEN, 1=YELLOW, 2=RED) |

### polymarket-strategy-advisor/ -- Trading Methodology

| Script | Purpose | Key Args |
|--------|---------|----------|
| `advisor.py` | Scan, score, size trade recommendations | `--top 5 --portfolio-db PATH` |
| `backtest.py` | Performance analysis + live-readiness assessment | `--live-check --days 30 --json` |
| `daily_review.py` | Win/loss breakdown, strategy analysis, suggestions | `--days 7` |

### polymarket-live-executor/ -- Real Trading (4 Safety Layers)

| Script | Purpose | Key Args |
|--------|---------|----------|
| `execute_live.py` | Place real orders (requires env vars + human "yes") | `--token-id ID --side BUY --size 5 --price 0.60` |
| `check_positions.py` | Wallet balance, open orders, trade history | `--balance --orders --trades` |
| `setup_wallet.py` | Create burner wallet, verify config, check balance | `--create / --verify / --check-balance` |

## CLAUDE.md Constitution

The repo includes a [CLAUDE.md](CLAUDE.md) that serves as the agent's trading constitution:

- **9 non-negotiable rules** (edge required, paper default, risk is law, human confirms live trades, etc.)
- **Authoritative risk limits** resolving all cross-file conflicts (graduated drawdown at 10/15/20%)
- **Daily workflow** (14-step session start -> scan -> evaluate -> execute -> review)
- **Paper-to-live prerequisites** (20+ trades, >55% win rate, Sharpe >0.5, drawdown <15%)
- **Experience tiers** for live capital progression ($25 -> $100 -> $500 -> $2,000+)

When CLAUDE.md conflicts with any SKILL.md, CLAUDE.md wins.

## Risk Management

### Risk Limits (from CLAUDE.md)

| Parameter | Limit |
|-----------|-------|
| Max position size | 10% of portfolio (5% if confidence < 0.7, 2% for news, 1% for new strategy) |
| Max concurrent positions | 5 |
| Max single market exposure | 20% of portfolio |
| Daily loss halt | 5% of portfolio |
| Weekly loss halt | 10% of portfolio |
| Graduated drawdown | 10% -> reduce 50%, 15% -> reduce 75%, 20% -> halt all |

### Correlation Detection

The `correlation_tracker.py` groups positions by topic and detects hidden correlation:

```
$ python polymarket-analyzer/scripts/correlation_tracker.py

  > Cluster: Insider Trading  (3 positions)  Exposure: $90.50 (9.4%)
    Correlation reason: shared qualifier: insider trading
    - YES $48.74  Will Axiom be accused of insider trading?
    - YES $23.90  Will MEXC be accused of insider trading?
    - YES $17.86  Will Robinhood be accused of insider trading?

  Diversification Score:  65/100  GOOD
```

### Portfolio Health Check

The `health_check.py` runs the full session-start workflow in one command:

```
$ python polymarket-paper-trader/scripts/health_check.py

  Status: [RED]    Action required
  Total Value:  $966.43    Drawdown: 3.36%
  Stops Triggered: 2
  [HIGH] Stop-loss triggered for MEXC insider trading
  [HIGH] Stop-loss triggered for Robinhood insider trading
```

### Live-Readiness Assessment

The `backtest.py --live-check` tells you when you're ready for real money:

```
$ python polymarket-strategy-advisor/scripts/backtest.py --live-check

  Live-Readiness Assessment: NOT READY (1/4 criteria met)
  [FAIL] Closed trades >= 20        actual: 0
  [FAIL] Win rate > 55%             actual: 0.0%
  [FAIL] Sharpe ratio > 0.5         actual: 0.0
  [PASS] Max drawdown < 15%         actual: 0.0%
```

## Going Live

### Prerequisites

1. 20+ closed paper trades with >55% win rate, Sharpe >0.5, drawdown <15%
2. A burner wallet (never your main wallet) funded with USDC on Polygon
3. Environment variables configured

### Setup

```bash
cd polymarket-live-executor

# Create a burner wallet
python scripts/setup_wallet.py --create

# Fund it: send $25 USDC + 0.1 MATIC on Polygon

# Configure
cp .env.example .env && chmod 600 .env
# Edit .env with your private key

# Verify
source .env
python scripts/setup_wallet.py --verify
python scripts/setup_wallet.py --check-balance
```

### Experience Tiers

| Level | Max Wallet | Max Per Trade | Daily Loss Limit |
|-------|-----------|---------------|------------------|
| First time | $25 | $5 | $10 |
| Learning | $100 | $10 | $25 |
| Experienced | $500 | $50 | $100 |
| Advanced | $2,000+ | $200 | $500 |

### Safety Layers

1. **Paper-first**: Must prove edge in simulation before going live
2. **Env var gate**: `POLYMARKET_CONFIRM=true` required for any execution
3. **Human-in-the-loop**: Every trade shows full details, waits for "yes"
4. **Position caps**: Hard limits on size, daily loss, and concentration

## APIs Used

| API | Endpoint | Auth | Usage |
|-----|----------|------|-------|
| Gamma API | `gamma-api.polymarket.com` | None | Market metadata, search |
| CLOB API | `clob.polymarket.com` | None (read) / L2 (trade) | Prices, orderbooks, trading |

No official Polymarket testnet exists. The paper trading engine simulates execution against real live prices.

## Data Storage

| Location | Contents |
|----------|----------|
| `~/.polymarket-paper/portfolio.db` | Paper trading portfolio (SQLite) |
| `~/.polymarket-live/trades.log` | Live trade log |
| `~/.polymarket-paper/` | All paper trading data |

## Security

- Full security audit: [SECURITY-AUDIT.md](SECURITY-AUDIT.md) (14 findings, all HIGH/MEDIUM fixed)
- All DB queries use parameterized statements (no SQL injection)
- Market data treated as untrusted (prompt injection defense)
- Private keys never logged, displayed, or echoed
- `.env` files gitignored; `.env.example` provided as template

## Disclaimer

- These skills provide analytical tools and a paper trading simulator
- Not financial advice. Past performance does not predict future results
- Prediction market trading involves risk of total loss
- Always paper trade new strategies before risking real capital
- The authors are not responsible for any trading losses
