# Money Trading Bot

Multi-asset market scanner and trading engine built with FastAPI, SQLite, and broker-aware services.

The project is API-first, paper-trading safe by default, and designed for learning, research, and paper execution. It does not guarantee profitability and should not be treated as investment advice.

## What It Does

- Syncs a broker-aware asset universe instead of relying on a tiny hardcoded stock list.
- Supports equities, ETFs, and supported crypto pairs.
- Keeps options behind a feature flag and paper-only guardrail.
- Normalizes bars, quotes, trades, snapshots, and session state across asset classes.
- Scans the tradable universe for gainers, losers, breakouts, pullbacks, volatility, momentum, and overall opportunities.
- Runs one explicitly configured active strategy at runtime.
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
- `app/strategies/`: strategy implementations plus active-strategy selection helpers.
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

- In `mock` mode, the asset universe comes from local symbol CSVs in `data/`.
- In `paper` mode, the asset catalog syncs from Alpaca paper and caches the results in SQLite.
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

Available strategies:

- equity/ETF momentum breakout
- equity/ETF trend pullback
- crypto momentum trend
- mean reversion scanner
- legacy EMA crossover support

Only one strategy is active at runtime. Set it with `ACTIVE_STRATEGY`, inspect it with `GET /config`, `GET /auto/status`, or `GET /diagnostics/strategy`, and do not rely on stale `ema_crossover` alerts from older wiring.

Signals are normalized with fields such as symbol, asset class, strategy name, direction, confidence score, entry, stop, target, ATR, momentum, liquidity, spread, regime, and reason.

## Paper Trading Safety

- `BROKER_MODE=paper` means Alpaca paper trading.
- `BROKER_MODE=mock` keeps everything local and CSV-backed.
- `TRADING_ENABLED=false` is the default.
- `AUTO_TRADE_ENABLED=false` is the default.
- `TRADING_ENABLED=true` enables actual Alpaca paper order submission when `BROKER_MODE=paper`.
- `AUTO_TRADE_ENABLED=true` starts the in-process auto-trader exactly once at API startup.
- Live trading stays disabled unless `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_ACK=ENABLE_LIVE_TRADING`.
- Alpaca live URLs are rejected unless live trading is explicitly enabled and acknowledged.
- Options remain feature-flagged and paper-only.
- `SHORT_SELLING_ENABLED=false` is the default.
- When short selling is disabled, bearish `SELL` signals are exit-only. If there is no tracked long position, the bot will not place a sell and will surface `no_position_to_sell` diagnostics instead of behaving like a short-entry engine.
- Daily loss and drawdown controls still block new exposure such as `BUY` orders, but real risk-reducing sells are allowed so the bot can exit losing longs.
- Position sizing uses `Decimal` math plus `POSITION_NOTIONAL_BUFFER_PCT` so auto-sized orders land safely under the hard `MAX_POSITION_NOTIONAL` cap instead of right on the boundary.

## Why Repeated Rejects Happen

Repeated paper-mode rejects usually mean the bot is still evaluating symbols after a loss threshold has already been crossed, or that the proposed size landed too close to a hard notional limit.

- `BUY` orders are correctly rejected once the current daily loss exceeds `MAX_DAILY_LOSS_PCT` or `MAX_DAILY_LOSS`.
- Before this fix, a bearish strategy could emit `SELL` for symbols with no tracked long position. Those looked like new exposure, so the risk manager rejected them under the daily-loss rule and Discord showed noisy `SELL ... rejected` alerts.
- The bot now treats bearish sells as exit-only when short selling is disabled. If no tracked long exists, the strategy returns `HOLD` for scan flow and execution/risk still reject direct sell attempts with the explicit rule `no_position_to_sell`.
- The bot now sizes `BUY` orders below the hard cap using `POSITION_NOTIONAL_BUFFER_PCT` and surfaces the raw qty, raw price, raw notional, rounded notional, and comparison operator in rejection diagnostics when a candidate still fails risk validation.

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
- paper vs mock behavior
- `TRADING_ENABLED` vs `AUTO_TRADE_ENABLED`
- active strategy selection
- position-notional sizing buffer
- short-selling guardrails
- Discord webhook notifications
- enabled asset classes
- universe refresh cadence
- watchlists and exclusions
- liquidity and spread thresholds
- risk and exposure caps
- strategy enable switches

Discord notification settings:

- `DISCORD_NOTIFICATIONS_ENABLED=false`
- `DISCORD_WEBHOOK_URL=` for the Discord webhook URL
- `DISCORD_NOTIFY_DRY_RUNS=false` to keep paper and dry-run alerts optional
- `DISCORD_NOTIFY_REJECTIONS=true`
- `DISCORD_NOTIFY_ERRORS=true`
- `DISCORD_NOTIFY_START_STOP=true`

If Discord notifications are enabled without a webhook URL, startup and settings validation fail fast with a clear error.

Important trading behavior settings:

- `BROKER_MODE=paper`
- `ACTIVE_STRATEGY=equity_momentum_breakout`
- `SHORT_SELLING_ENABLED=false`
- `POSITION_NOTIONAL_BUFFER_PCT=0.995`
- `MAX_DAILY_LOSS=2000`
- `MAX_DAILY_LOSS_PCT=0.02`
- `MAX_DRAWDOWN_PCT=0.10`

Paper-mode setup:

- Use `BROKER_MODE=paper` with Alpaca paper API keys in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
- Use `BROKER_MODE=mock` only for local CSV-backed testing without Alpaca.
- `TRADING_ENABLED=true` submits Alpaca paper orders.
- `AUTO_TRADE_ENABLED=true` starts the continuous in-process auto-trader loop.
- `ACTIVE_STRATEGY` is the only runtime strategy selector. The old `STRATEGY_NAME` env var is accepted as a compatibility alias, but the app normalizes it to `ACTIVE_STRATEGY`.

## Discord Notifications

The bot can send targeted Discord webhook notifications for meaningful trading events:

- submitted orders
- optional dry-run orders
- optional risk rejections
- auto-trader start and stop events
- auto-trader cycle failures

It does not mirror the full application log to Discord, and it does not send notifications for `HOLD` signals or normal scan heartbeats.

### Create a Webhook

1. In Discord, open the channel where you want notifications.
2. Open `Edit Channel`, then `Integrations`, then `Webhooks`.
3. Create a webhook and copy the webhook URL.
4. Set `DISCORD_WEBHOOK_URL` in `.env`.

Example configuration:

```env
DISCORD_NOTIFICATIONS_ENABLED=true
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_webhook_id/your_webhook_token
DISCORD_NOTIFY_DRY_RUNS=true
DISCORD_NOTIFY_REJECTIONS=true
DISCORD_NOTIFY_ERRORS=true
DISCORD_NOTIFY_START_STOP=true
```

`DISCORD_NOTIFY_DRY_RUNS` is optional and defaults to `false` so local testing and paper-mode scans do not spam Discord unless you explicitly opt in.

Rejected trade notifications now include the exact risk rule, whether the symbol had a tracked position, whether a rejected sell was risk-reducing, the active strategy, current equity vs daily baseline context, and the raw/rounded notional sizing details when the candidate failed on exposure rules.

## Diagnostics

Use the diagnostics routes to understand why the bot is blocking new exposure, whether the auto-trader is really running, and whether local portfolio tracking matches the broker:

- `GET /diagnostics/auto`
- `GET /diagnostics/risk`
- `GET /diagnostics/strategy`
- `GET /diagnostics/portfolio`
- `GET /diagnostics/rejections/latest`

These routes expose:

- trading enabled, auto-trade enabled, broker mode, broker backend, and active strategy
- market-open status and extended-hours allowance
- account cash, equity, and buying power
- daily baseline equity and date
- current daily loss amount and percent
- drawdown percent
- active symbol and strategy cooldowns
- latest evaluated symbols and latest signals
- latest accepted and rejected order candidates
- local tracked positions with `is_long` and `sellable`
- broker-reported positions
- latest risk events
- latest rejection rule and reason

## Resetting Local State

Use the API or the helper script to restart the bot's local paper-trading state.

API:

```bash
curl -X POST http://127.0.0.1:8000/admin/reset-local-state
curl -X POST http://127.0.0.1:8000/admin/reset-local-state \
  -H "Content-Type: application/json" \
  -d '{"close_positions":true,"cancel_open_orders":true,"wipe_local_db":true,"reset_daily_baseline_to_current_equity":true}'
```

Script:

```bash
source .venv/bin/activate
python scripts/reset_local_state.py
python scripts/reset_local_state.py --wipe-local-db
```

The reset flow:

- stops the auto-trader if it is running
- clears in-memory portfolio state, cooldowns, cached rejections, and auto-trader debug state
- can cancel open paper orders and close paper positions
- can wipe the local SQLite history by dropping and recreating the schema

## How To Fully Reset Alpaca Paper Trading

Local bot reset and Alpaca paper-account reset are separate operations.

- The app can reset its own local runtime state and local SQLite history.
- The app does not claim to erase Alpaca's remote paper-trading history through the API.
- If you want a truly fresh Alpaca paper account, use the Alpaca dashboard to create a fresh paper account or remove the old paper account, then generate new paper API credentials for that paper account.
- A practical workflow is:
  1. Open the Alpaca dashboard.
  2. Create a new paper account or delete the old paper account there.
  3. Generate fresh paper API keys for the new paper account.
  4. Update `.env` with the new `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and `BROKER_MODE=paper`.
  5. Restart the API so the runtime reconnects to the new paper account.
- After creating the fresh paper account, update `.env` with the new `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and keep `ALPACA_BASE_URL=https://paper-api.alpaca.markets`.
- Restart the app after updating credentials so the runtime picks up the new paper account.

## Running Locally

Recommended stable run path for continuous paper auto-trading:

```bash
source .venv/bin/activate
python scripts/run_paper_api.py
```

This starts Uvicorn in a single process so the in-process auto-trader runs once. The logs now make startup and shutdown explicit with lines such as `Paper auto-trader is running` and `Paper auto-trader stopped`.

Direct Uvicorn run:

```bash
source .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000
```

Run tests:

```bash
source .venv/bin/activate
uv run pytest
```

Test Discord notifications locally:

```bash
source .venv/bin/activate
export DISCORD_NOTIFICATIONS_ENABLED=true
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/your_webhook_id/your_webhook_token"
export DISCORD_NOTIFY_DRY_RUNS=true
python scripts/run_paper_api.py
```

Then in another terminal:

```bash
source .venv/bin/activate
curl -X POST http://127.0.0.1:8000/run-once -H "Content-Type: application/json" -d '{"symbol":"AAPL","asset_class":"equity"}'
curl -X POST http://127.0.0.1:8000/auto/start
sleep 2
curl -X POST http://127.0.0.1:8000/auto/stop
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
- `GET /diagnostics/auto`
- `GET /diagnostics/strategy`
- `GET /diagnostics/strategies`
- `GET /diagnostics/risk`
- `GET /diagnostics/portfolio`
- `GET /diagnostics/rejections/latest`
- `POST /admin/reset-local-state`

Legacy compatibility:

- `POST /run-once`
- `POST /backtest`
- `GET /strategy/signals`
- `GET /strategy/positions`

## Verification Commands

Assuming the API is running on `127.0.0.1:8000`:

```bash
uv run pytest
python scripts/run_paper_api.py
uvicorn main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/config
curl http://127.0.0.1:8000/auto/status
curl http://127.0.0.1:8000/assets/stats
curl "http://127.0.0.1:8000/assets/search?q=BTC"
curl "http://127.0.0.1:8000/market/snapshot?symbol=AAPL&asset_class=equity"
curl "http://127.0.0.1:8000/market/snapshot?symbol=BTC/USD&asset_class=crypto"
curl "http://127.0.0.1:8000/scanner/overview?limit=5"
curl "http://127.0.0.1:8000/scanner/asset-class/crypto?limit=5"
curl -X POST http://127.0.0.1:8000/signals/run -H "Content-Type: application/json" -d '{"symbol":"AAPL","asset_class":"equity"}'
curl -X POST http://127.0.0.1:8000/auto/start
curl -X POST http://127.0.0.1:8000/auto/run-now
curl http://127.0.0.1:8000/signals/top
curl http://127.0.0.1:8000/risk
curl http://127.0.0.1:8000/diagnostics/auto
curl http://127.0.0.1:8000/diagnostics/strategy
curl http://127.0.0.1:8000/diagnostics/risk
curl http://127.0.0.1:8000/diagnostics/portfolio
curl http://127.0.0.1:8000/diagnostics/rejections/latest
curl -X POST http://127.0.0.1:8000/admin/reset-local-state
```

## Mock Mode Notes

The repository includes local mock CSVs for:

- `AAPL`
- `SPY`
- `QQQ`
- `BTC/USD`
- `ETH/USD`

In mock mode, the catalog and scanner treat those as the supported universe, and `TRADING_ENABLED=true` only affects the local in-memory broker. Use `BROKER_MODE=paper` when you want real Alpaca paper orders instead of the mock broker.

## Broker Limitations

- Broker coverage is limited to what the configured provider exposes.
- ETF classification in broker mode may depend on provider metadata and lightweight heuristics.
- Options support is feature-flagged, limited, and intentionally conservative.
- Large live universes may require tighter filters or scan limits to stay within provider rate limits.

## Disclaimer

This project is for learning, experimentation, and paper trading. It does not promise profits or reliable market performance.
