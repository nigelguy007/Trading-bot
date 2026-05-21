# Prediction Market Trading Bot

A Claude-powered AI agent swarm for automated prediction market trading on **Polymarket** and **Kalshi**.

## Architecture

```
Market Scanner → Sentiment Swarm → Edge Detector → Risk Agent → Execution Agent
```

### Agent Pipeline

| Agent | Role |
|---|---|
| **Market Scanner** | Scans Polymarket + Kalshi, filters by liquidity/volume/spread |
| **Sentiment Agent** | Twitter, Reddit sentiment → bullish/bearish narrative |
| **Edge Detector** | Kelly Criterion + Claude probability estimate |
| **Risk Agent** | Hard limits: drawdown, position size, exposure caps |
| **Execution Agent** | Places orders via REST APIs (dry-run or live) |

## Folder Structure

```
├── agents/
│   ├── market_scanner.py     # Filter pipeline + Claude market analysis
│   ├── sentiment_agent.py    # Twitter/Reddit → sentiment score
│   ├── edge_detector.py      # Kelly Criterion + EV calculation
│   ├── risk_agent.py         # Hard risk constraints
│   └── execution_agent.py    # Order routing
├── services/
│   ├── polymarket_api.py     # Polymarket CLOB API
│   ├── kalshi_api.py         # Kalshi REST API v2
│   ├── twitter_scraper.py    # Twitter v2 API
│   └── reddit_scraper.py     # Reddit public API
├── models/
│   └── probability_model.py  # Bayesian calibration
├── orchestrator/
│   └── workflow.py           # Full pipeline orchestrator
├── backend/
│   ├── main.py               # FastAPI app
│   ├── database.py           # PostgreSQL + Redis
│   └── routes/               # REST endpoints
├── frontend/                 # Next.js dashboard
├── scripts/
│   ├── run_bot.py            # One-shot CLI runner
│   └── scheduler.py          # Cron scheduler (15-min cycles)
└── tests/                    # Unit tests (no API calls)
```

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
# Fill in your API keys
```

### 2. Start with Docker Compose

```bash
docker-compose up -d
```

- Backend: http://localhost:8000
- Dashboard: http://localhost:3000
- API docs: http://localhost:8000/docs

### 3. Run manually (dry-run)

```bash
pip install -r requirements.txt
python scripts/run_bot.py --bankroll 1000
```

### 4. Run tests

```bash
pip install pytest
pytest tests/ -v
```

## Risk Controls (defaults)

| Parameter | Default |
|---|---|
| Max daily loss | $500 |
| Max position size | $200 |
| Max portfolio exposure | $2,000 |
| Min edge threshold | 5% |
| Min confidence score | 65% |
| Min market liquidity | $10,000 |

All overridable via `.env`.

## API Keys Required

| Service | Purpose | URL |
|---|---|---|
| Anthropic | Claude agent calls | console.anthropic.com |
| Polymarket | Market data + order execution | polymarket.com |
| Kalshi | Market data + order execution | kalshi.com |
| Twitter/X | Sentiment scraping | developer.twitter.com |

Reddit scraping uses the public JSON API — no key required.

## Edge Detection Formula

```
edge = model_probability - market_implied_probability
kelly_fraction = (b*p - q) / b   # capped at 25%
expected_value = p * (1/market_price) - 1
```

Where `b = (1/market_price) - 1`, `p` = model probability, `q = 1 - p`.

## Important Disclaimer

Prediction markets are competitive. Real profitability depends on execution
quality, data freshness, bankroll management, and persistent inefficiencies.
Always run in dry-run mode first and never risk capital you can't afford to lose.
