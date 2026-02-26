# Polymarket AI Trading Skills

Composable [Agent Skills](https://agentskills.io/specification) for Polymarket prediction market trading. Paper-trading-first, security-audited, works with Claude Code, OpenClaw, NanoClaw, Codex, Cursor, and any SKILL.md-compatible agent.

## Skills

| Skill | What It Does | Auth Required | Risk |
|-------|-------------|---------------|------|
| **polymarket-scanner** | Browse, search, and explore live markets | None | Zero |
| **polymarket-analyzer** | Detect edges: arbitrage, momentum, wide spreads | None | Zero |
| **polymarket-monitor** | Price alerts and position monitoring | None | Zero |
| **polymarket-paper-trader** | Simulate trades against live prices (zero risk) | None | Zero |
| **polymarket-strategy-advisor** | Trading methodology, recommendations, daily review | None | Low |
| **polymarket-live-executor** | Execute real trades (requires wallet + explicit opt-in) | L2 Wallet | Medium |

## Install

### Claude Code / Compatible Agents
```bash
npx skills add https://github.com/verticalclaw/polymarket-skills --skill polymarket-scanner
npx skills add https://github.com/verticalclaw/polymarket-skills --skill polymarket-analyzer
npx skills add https://github.com/verticalclaw/polymarket-skills --skill polymarket-monitor
npx skills add https://github.com/verticalclaw/polymarket-skills --skill polymarket-paper-trader
npx skills add https://github.com/verticalclaw/polymarket-skills --skill polymarket-strategy-advisor
npx skills add https://github.com/verticalclaw/polymarket-skills --skill polymarket-live-executor
```

### Manual
Copy any skill folder to `~/.claude/skills/` (or your agent's skill directory).

### Dependencies
```bash
pip install py-clob-client
```

## Quick Start

Once installed, just talk to your agent naturally:

- *"Scan Polymarket for interesting markets"* — triggers polymarket-scanner
- *"Find trading opportunities"* — triggers polymarket-analyzer
- *"Set up a paper trading portfolio with $1000"* — triggers polymarket-paper-trader
- *"What should I trade?"* — triggers polymarket-strategy-advisor
- *"Check my portfolio"* — triggers polymarket-paper-trader

### Full Pipeline Example

```bash
# 1. Scan markets
python polymarket-scanner/scripts/scan_markets.py --limit 20 --min-volume 50000

# 2. Find edges
python polymarket-analyzer/scripts/find_edges.py --limit 30

# 3. Get recommendations
python polymarket-strategy-advisor/scripts/advisor.py --top 5 --portfolio-db ~/.polymarket-paper/portfolio.db

# 4. Paper trade
python polymarket-paper-trader/scripts/paper_engine.py --action buy --token TOKEN_ID --side YES --size 50 --reason "Momentum signal"

# 5. Review performance
python polymarket-strategy-advisor/scripts/daily_review.py --portfolio-db ~/.polymarket-paper/portfolio.db
```

## Architecture

```
polymarket-scanner/          # Read-only market data (Gamma + CLOB APIs)
  scripts/scan_markets.py    # Browse/search markets
  scripts/get_orderbook.py   # Full order book depth
  scripts/get_prices.py      # Current prices/spreads

polymarket-analyzer/         # Edge detection
  scripts/find_edges.py      # Arbitrage, overpriced, wide spread detection
  scripts/momentum_scanner.py # Volume surge + orderbook imbalance signals
  scripts/analyze_orderbook.py # Depth analysis

polymarket-paper-trader/     # Zero-risk simulation engine
  scripts/paper_engine.py    # Core engine (SQLite portfolio)
  scripts/execute_paper.py   # Execute strategy recommendations
  scripts/portfolio_report.py # Sharpe, Sortino, drawdown analytics

polymarket-strategy-advisor/ # Trading methodology
  scripts/advisor.py         # Scan + score + size recommendations
  scripts/daily_review.py    # Performance review + suggestions

polymarket-monitor/          # Position monitoring
  scripts/monitor_prices.py  # Multi-token price polling with alerts
  scripts/watch_market.py    # Continuous single-market snapshots

polymarket-live-executor/    # Real trading (4 safety layers)
  scripts/execute_live.py    # Requires wallet + POLYMARKET_CONFIRM=true
  scripts/check_positions.py # Balance, orders, trade history
```

## Safety Design

- **Paper trading first**: Every strategy runs in simulation before real capital
- **No wallet by default**: Scanner, analyzer, monitor, paper trader, and advisor need zero authentication
- **Live executor locked**: Requires `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_CONFIRM=true` + per-trade "yes" confirmation
- **Risk engine**: Position limits, drawdown stops, daily loss limits, concentration caps
- **Prompt injection protection**: All market data sanitized before display (market names are user-generated content)
- **SQL injection prevention**: All queries use parameterized statements
- **Security audited**: See [SECURITY-AUDIT.md](SECURITY-AUDIT.md)

## APIs Used

| API | Endpoint | Auth | Usage |
|-----|----------|------|-------|
| Gamma API | `gamma-api.polymarket.com` | None | Market metadata, search |
| CLOB API | `clob.polymarket.com` | None (read) / L2 (trade) | Prices, orderbooks, trading |

## Disclaimer

- These skills provide analytical tools and a paper trading simulator
- Not financial advice. Past performance does not predict future results
- Prediction market trading involves risk of total loss
- Always paper trade new strategies before risking real capital
- The authors are not responsible for any trading losses
