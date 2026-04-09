# Money Trading Bot

A beginner-friendly algorithmic trading skeleton for paper trading only.

This project provides a safe, deterministic trading system structure with:

- clean architecture
- risk controls
- local persistence with SQLite
- a small FastAPI control surface
- sample EMA crossover strategy
- mock paper broker behavior by default

## Project structure

- `app/` - main application packages
  - `config/` - settings and environment loading
  - `data/` - market data helpers and CSV loaders
  - `strategies/` - trading strategy implementations
  - `risk/` - risk management and guardrails
  - `execution/` - order execution and signal handling
  - `portfolio/` - portfolio state tracking and metrics
  - `monitoring/` - logging utilities
  - `db/` - SQLAlchemy models and database initialization
  - `api/` - FastAPI routes and application startup
  - `services/` - broker, market data, and backtest services
- `backtests/` - backtest utilities and helper modules
- `tests/` - pytest test coverage
- `scripts/` - simple CLI helpers
- `main.py` - FastAPI entrypoint

## Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Configuration

Update `.env` with your environment values. By default, the bot runs in paper trading mode and trading is disabled unless `TRADING_ENABLED=true`.

Required environment variables:

- `APP_ENV` - environment name (development, staging, production)
- `LOG_LEVEL` - log verbosity
- `DATABASE_URL` - sqlite or database connection string
- `BROKER_MODE` - `paper` by default
- `TRADING_ENABLED` - `false` by default
- `MAX_RISK_PER_TRADE` - maximum risk per trade as a decimal
- `MAX_DAILY_LOSS_PCT` - daily loss limit as a decimal
- `MAX_DRAWDOWN_PCT` - drawdown limit as a decimal
- `MAX_POSITIONS` - maximum concurrent positions
- `DEFAULT_TIMEFRAME` - example value `1D`
- `DEFAULT_SYMBOLS` - JSON array of default symbols, e.g., `["AAPL","SPY"]`

Optional Alpaca variables for future use:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_BASE_URL`

## How to connect Alpaca paper trading

1. Create an Alpaca paper trading account at https://app.alpaca.markets.
2. Generate paper API keys in the Alpaca dashboard.
3. Copy `.env.example` to `.env`.
4. Set `BROKER_MODE=alpaca`.
5. Add your `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` to `.env`.
6. Keep `TRADING_ENABLED=false` until you are ready to test with paper orders.

Example `.env` values for Alpaca paper mode:

```env
BROKER_MODE=alpaca
TRADING_ENABLED=false
ALPACA_API_KEY=your-paper-key
ALPACA_SECRET_KEY=your-paper-secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
DEFAULT_SYMBOLS=["AAPL","SPY"]
```

### Testing Alpaca integration safely

- Start the API in dry-run mode first.
- Verify `GET /broker/status` before placing orders.
- Use `POST /run-once` to evaluate signals and risk without executing trades.
- Only set `TRADING_ENABLED=true` when you want live paper order submission.

> Warning: paper trading is not the same as live trading. Results may differ in a real market environment.

## Initialize database

```bash
python scripts/init_db.py
```

## Run the API

```bash
uvicorn main:app --reload
```

API endpoints:

- `GET /health`
- `GET /config`
- `GET /broker/status`
- `GET /broker/account`
- `GET /positions`
- `GET /orders`
- `GET /trades`
- `GET /risk`
- `POST /run-once`
- `POST /backtest`

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
```

### Environment Variable Notes

- `DEFAULT_SYMBOLS` must be a valid JSON array, e.g., `["AAPL","SPY"]`. Comma-separated strings are not supported.
- For Alpaca mode, ensure `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set. If authentication fails, `/broker/account` will return a 401 error with details.
- VS Code's integrated terminal may not automatically load `.env` files; ensure your environment variables are set or source the `.env` manually if needed.

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
