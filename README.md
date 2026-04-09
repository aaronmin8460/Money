# Money Trading Bot

A paper-trading algorithmic trading system built around FastAPI, Alpaca paper market access, and a risk-aware trading pipeline.

This repository now includes:

- A regime-filtered momentum breakout strategy for paper trading
- Alpaca paper trading endpoints separated from market data endpoints
- Risk-based position sizing with ATR stop placement
- Safe auto-trading loop with cooldowns, market-open checks, and ranked entries
- SQLite persistence for signals, orders, and auto-trader run history
- API control surface for monitoring and manual orchestration

## What changed

The strategy now uses:

- SPY regime filter based on 50/200-day SMA crossover
- Breakout entries above the prior 20-day high
- Trend confirmation via 20 EMA, 50 SMA, and 100 SMA
- Volume confirmation relative to 20-day average volume
- Momentum ranking across the configured universe
- ATR-based initial stops and trailing stop logic
- Volatility-adjusted position sizing based on account equity

This is paper trading only. It should not be interpreted as investment advice or a guarantee of profitability.

## Project structure

- `app/` - main application packages
- `app/api/` - FastAPI routes and app startup
- `app/config/` - settings and environment loading
- `app/db/` - SQLAlchemy models and initialization
- `app/portfolio/` - portfolio tracking and reconciliation
- `app/risk/` - risk management guardrails
- `app/execution/` - order execution and signal processing
- `app/services/` - broker interface, market data, backtests, auto-trader
- `app/strategies/` - trading strategies
- `tests/` - pytest coverage
- `main.py` - FastAPI entrypoint

## Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Create or update `.env` with the following values:

```ini
APP_ENV=development
LOG_LEVEL=INFO
DATABASE_URL=sqlite:///./trading.db
BROKER_MODE=paper
TRADING_ENABLED=false
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_BASE_URL=https://data.alpaca.markets
DEFAULT_SYMBOLS=["AAPL","SPY","QQQ"]
MAX_RISK_PER_TRADE=0.01
MAX_POSITIONS=3
MAX_POSITION_NOTIONAL=10000
COOLDOWN_SECONDS_PER_SYMBOL=300
ALLOW_EXTENDED_HOURS=false
```

### Important note

- `ALPACA_BASE_URL` is used for trading/account/order endpoints.
- `ALPACA_DATA_BASE_URL` is used for Alpaca market data bar requests.
- The system runs in paper trading mode only and does not guarantee profits.

## Running the API

```bash
uvicorn main:app --reload
```

## Auto-trader endpoints

- `GET /auto/status`
- `POST /auto/start`
- `POST /auto/stop`
- `POST /auto/run-now`

## Broker and strategy endpoints

- `GET /broker/status`
- `GET /broker/account`
- `GET /positions`
- `GET /orders`
- `GET /strategy/signals`
- `GET /strategy/positions`

## Dry-run vs paper order submission

When `TRADING_ENABLED=false`, the bot will still evaluate signals and position size but will not submit real Alpaca paper orders. This is the safe default for testing.

## Testing

```bash
pytest
```

## Disclaimer

This code is designed for learning and paper trading. Paper results are not a guarantee of future performance.

- `GET /orders`
- `GET /trades`
- `GET /risk`
- `POST /run-once`
- `POST /backtest`
- `GET /auto/status`
- `POST /auto/start`
- `POST /auto/stop`
- `POST /auto/run-now`

## Local Development and Verification

After starting the API with `uvicorn main:app --reload`, verify the setup:

```bash
# Check health
curl http://127.0.0.1:8000/health

# Check config (note DEFAULT_SYMBOLS is JSON)
curl http://127.0.0.1:8000/config

# Check broker status
curl http://127.0.0.1:8000/broker/status

# Check broker account (in paper mode, should return mock data)
curl http://127.0.0.1:8000/broker/account

# Check auto-trader status
curl http://127.0.0.1:8000/auto/status

# Run a manual scan
curl -X POST http://127.0.0.1:8000/auto/run-now

# Start auto-trading (if AUTO_TRADE_ENABLED=true)
curl -X POST http://127.0.0.1:8000/auto/start

# Stop auto-trading
curl -X POST http://127.0.0.1:8000/auto/stop
```

### Environment Variable Notes

- `DEFAULT_SYMBOLS` must be a valid JSON array, e.g., `["AAPL","SPY"]`. Comma-separated strings are not supported.
- For Alpaca mode, ensure `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set. If authentication fails, `/broker/account` will return a 401 error with details.
- `ALPACA_BASE_URL` is for trading operations (account, orders, positions).
- `ALPACA_DATA_BASE_URL` is for market data (bars, quotes).
- VS Code's integrated terminal may not automatically load `.env` files; ensure your environment variables are set or source the `.env` manually if needed.

## Auto-Trading

The bot can run automated trading cycles:

1. **Manual Run**: Use `POST /auto/run-now` to execute one scan cycle immediately.
2. **Automated**: Set `AUTO_TRADE_ENABLED=true` and use `POST /auto/start` to begin periodic scanning.
3. **Status**: Use `GET /auto/status` to check the current state.

### Safety Features

- Only trades during market hours (unless `ALLOW_EXTENDED_HOURS=true`).
- Respects `TRADING_ENABLED=false` for dry-run mode.
- Symbol-level cooldowns prevent over-trading.
- Position sizing based on buying power and risk limits.
- Reconciliation ensures local and broker state sync.

### Testing Auto-Trading Safely

- Start with `BROKER_MODE=paper` and `TRADING_ENABLED=false`.
- Use `POST /auto/run-now` to test signal generation without orders.
- Enable `TRADING_ENABLED=true` only for paper orders.
- Monitor logs and `/auto/status` for activity.

## Run tests

```bash
pytest
```

## Run a sample backtest

```bash
python scripts/run_backtest.py --symbol SPY --csv-path data/sample.csv
```

## Safety notes

- This project is configured for paper trading only by default.
- No live trading is enabled unless `TRADING_ENABLED=true` and a paper broker is explicitly configured.
- Real broker credentials are never hardcoded.
- Paper trading behavior is a safe simulation and does not guarantee identical results in live markets.
