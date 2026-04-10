# Money Trading Bot

Multi-asset market scanner and trading engine built with FastAPI, SQLite, and broker-aware services.

The project is API-first, paper-trading safe by default, and designed for learning, research, and paper execution. It does not guarantee profitability and should not be treated as investment advice.

## What It Does

- Syncs a broker-aware asset universe instead of relying on a tiny hardcoded stock list.
- Supports equities, ETFs, and supported crypto pairs.
- Keeps options behind a feature flag and paper-only guardrail.
- Normalizes bars, quotes, trades, snapshots, and session state across asset classes.
- Scans the tradable universe for gainers, losers, breakouts, pullbacks, volatility, momentum, and overall opportunities.
- Routes strategies by asset class and ranks generated signals.
- Applies multi-asset risk controls before any order placement.
- Persists catalog syncs, scanner runs, opportunities, signals, orders, fills, position snapshots, and bot runs in SQLite.

## Architecture

- `app/api/`: FastAPI routers for assets, market data, scanner, signals, broker, automation, and diagnostics.
- `app/config/`: environment-backed settings and feature flags.
- `app/db/`: SQLAlchemy models and schema initialization.
- `app/domain/`: normalized asset, market data, session, and opportunity types.
- `app/services/asset_catalog.py`: broker-aware asset universe sync and cache.
- `app/services/market_data.py`: normalized bars, quotes, trades, snapshots, and session behavior.
- `app/services/scanner.py`: multi-asset ranking engine and scanner persistence.
- `app/services/market_overview.py`: overview summaries built from scanner output.
- `app/strategies/`: asset-class-specific strategies plus registry/routing.
- `app/risk/`: exposure, liquidity, spread, drawdown, cooldown, and kill-switch controls.
- `app/execution/`: normalized signal-to-order flow with dry-run and paper safety.
- `app/services/auto_trader.py`: automation loop for scanning, signal ranking, and execution.

## Supported Asset Classes

- `equity`
- `etf`
- `crypto`
- `option`
  Options remain disabled by default and are limited to feature-flagged, paper-only handling.

## Universe Discovery

`All markets` in this project means all tradable assets supported by the configured broker or data provider.

- In `paper` / `mock` mode, the asset universe comes from local symbol CSVs in `data/`.
- In `alpaca` mode, the asset catalog syncs from the broker asset list and caches the results in SQLite.
- The catalog stores symbol, name, asset class, exchange, tradable flags, borrow flags, margin flags, and raw attributes.
- Universe scanning can be narrowed with watchlists, inclusion lists, exclusion lists, and enabled asset-class switches.

## Scanning and Signals

The scanner produces ranked views for:

- top gainers
- top losers
- unusual volume
- breakout candidates
- pullback candidates
- high volatility
- momentum
- overall opportunities

Strategies currently included:

- equity/ETF momentum breakout
- equity/ETF trend pullback
- crypto momentum trend
- mean reversion scanner
- legacy EMA crossover support

Signals are normalized with fields such as symbol, asset class, strategy name, direction, confidence score, entry, stop, target, ATR, momentum, liquidity, spread, regime, and reason.

## Paper Trading Safety

- Default broker mode is paper/mock safe.
- `TRADING_ENABLED=false` is the default.
- Live trading stays disabled unless `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_ACK=ENABLE_LIVE_TRADING`.
- Alpaca live URLs are rejected unless live trading is explicitly enabled and acknowledged.
- Options remain feature-flagged and paper-only.

## Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Configuration

Start from `.env.example`. Important categories:

- broker mode and credentials
- paper vs live gating
- enabled asset classes
- universe refresh cadence
- watchlists and exclusions
- liquidity and spread thresholds
- risk and exposure caps
- strategy enable switches

## Running Locally

Initialize and run:

```bash
source .venv/bin/activate
uvicorn main:app --reload
```

Run tests:

```bash
source .venv/bin/activate
pytest
```

## Key Endpoints

Assets:

- `GET /assets`
- `GET /assets/search`
- `GET /assets/{symbol}`
- `POST /assets/refresh`
- `GET /assets/stats`

Market data:

- `GET /market/bars`
- `GET /market/quote`
- `GET /market/trade`
- `GET /market/snapshot`
- `GET /market/session`

Scanner:

- `GET /scanner/overview`
- `GET /scanner/top-gainers`
- `GET /scanner/top-losers`
- `GET /scanner/breakouts`
- `GET /scanner/momentum`
- `GET /scanner/volatility`
- `GET /scanner/opportunities`
- `GET /scanner/asset-class/{asset_class}`

Signals:

- `GET /signals`
- `POST /signals/run`
- `GET /signals/top`

Trading and automation:

- `GET /auto/status`
- `POST /auto/start`
- `POST /auto/stop`
- `POST /auto/run-now`
- `GET /orders`
- `GET /positions`
- `GET /trades`
- `GET /risk`
- `GET /broker/account`
- `GET /broker/status`

Diagnostics:

- `GET /health`
- `GET /config`
- `GET /diagnostics/universe`
- `GET /diagnostics/data-feed`
- `GET /diagnostics/strategies`

Legacy compatibility:

- `POST /run-once`
- `POST /backtest`
- `GET /strategy/signals`
- `GET /strategy/positions`

## Verification Commands

Assuming the API is running on `127.0.0.1:8000`:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/config
curl http://127.0.0.1:8000/assets/stats
curl "http://127.0.0.1:8000/assets/search?q=BTC"
curl "http://127.0.0.1:8000/market/snapshot?symbol=AAPL&asset_class=equity"
curl "http://127.0.0.1:8000/market/snapshot?symbol=BTC/USD&asset_class=crypto"
curl "http://127.0.0.1:8000/scanner/overview?limit=5"
curl "http://127.0.0.1:8000/scanner/asset-class/crypto?limit=5"
curl -X POST http://127.0.0.1:8000/signals/run -H "Content-Type: application/json" -d '{"symbol":"AAPL","asset_class":"equity"}'
curl -X POST http://127.0.0.1:8000/auto/run-now
curl http://127.0.0.1:8000/signals/top
curl http://127.0.0.1:8000/risk
```

## Mock Mode Notes

The repository includes local mock CSVs for:

- `AAPL`
- `SPY`
- `QQQ`
- `BTC/USD`
- `ETH/USD`

In mock mode, the catalog and scanner treat those as the supported universe.

## Broker Limitations

- Broker coverage is limited to what the configured provider exposes.
- ETF classification in broker mode may depend on provider metadata and lightweight heuristics.
- Options support is feature-flagged, limited, and intentionally conservative.
- Large live universes may require tighter filters or scan limits to stay within provider rate limits.

## Disclaimer

This project is for learning, experimentation, and paper trading. It does not promise profits or reliable market performance.
