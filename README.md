# Money

FastAPI-based trading bot for paper-safe operation, structured logging, Discord notifications, optional ML scoring, and offline research workflows. The project is intentionally biased toward safe paper trading first, with guarded live trading left as an explicit opt-in.

## 1. Project status and intended use

- Intended use: local development, paper trading, and production-style paper deployments.
- Current posture: paper-safe defaults are intentional and `.env.example` contains placeholders only.
- Preferred runtime: `scripts/run_paper_api.py` keeps the FastAPI API and in-process auto-trader on a single process.
- Non-goals for safe operation: ML is optional, news is feature-only, and RL stays sandbox-only.

## 2. Safety model and guardrails

- Default mode is `BROKER_MODE=paper`.
- `.env.example` ships with `TRADING_ENABLED=false`, `AUTO_TRADE_ENABLED=false`, `LIVE_TRADING_ENABLED=false`, `DISCORD_NOTIFICATIONS_ENABLED=false`, `ML_ENABLED=false`, `ML_RETRAIN_ENABLED=false`, `NEWS_FEATURES_ENABLED=false`, and `ALLOW_EXTENDED_HOURS=false`.
- Live trading requires explicit acknowledgement through both `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_ACK=ENABLE_LIVE_TRADING`.
- ML can assist ranking and filtering, but it must not bypass hard stops, drawdown limits, or emergency exits.
- News features can enrich signals, but they never place orders directly.
- RL code remains offline and must not connect to paper or live execution.

## 3. Discord-only notification policy

- Discord is the only supported alert channel in this repository.
- No Slack, Telegram, email, or SMS integrations are part of the supported ops posture.
- Discord notifications are optional and disabled by default.
- Replay suppression exists through:
  - short-term notification dedupe with `DISCORD_DEDUPE_TTL_SECONDS`
  - broker lifecycle replay suppression persisted at `BROKER_ORDER_STATUS_CACHE_PATH`
- Startup and shutdown alerts, rejection alerts, error alerts, and compact scan summaries are all routed through the Discord notifier when enabled.

## 4. Runtime modes: dev / paper / guarded live

### Dev

- Typical settings: `APP_ENV=development`, `BROKER_MODE=mock` or `paper`, `TRADING_ENABLED=false`, `AUTO_TRADE_ENABLED=false`.
- Goal: startup validation, route checks, and manual `run-once` evaluation.
- Admin auth can be omitted only if you do not need protected admin or diagnostics routes locally.

### Paper

- Typical settings: `APP_ENV=production`, `BROKER_MODE=paper`, `TRADING_ENABLED=true`, `AUTO_TRADE_ENABLED=true`, `LIVE_TRADING_ENABLED=false`.
- Goal: single-process paper deployment with Alpaca paper credentials and optional Discord alerts.
- Recommended for shared or persistent environments: set `API_ADMIN_TOKEN` and use Postgres for `DATABASE_URL`.

### Guarded live

- Not the default and not recommended unless you explicitly accept the risk.
- Requires all paper guardrails plus `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_ACK=ENABLE_LIVE_TRADING`.
- Review risk controls and deployment posture carefully before enabling it.

## 5. Required environment variables

Required for most local and paper deployments:

- `BROKER_MODE`
- `DATABASE_URL`
- `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` when `BROKER_MODE=paper`
- `API_ADMIN_TOKEN` for any environment where protected admin or diagnostics routes should be reachable

Required only when the related feature is enabled:

- `DISCORD_WEBHOOK_URL` when `DISCORD_NOTIFICATIONS_ENABLED=true`
- `OPENAI_API_KEY` when both `NEWS_FEATURES_ENABLED=true` and `NEWS_LLM_ENABLED=true`
- `LIVE_TRADING_ACK=ENABLE_LIVE_TRADING` when `LIVE_TRADING_ENABLED=true`

Database guidance:

- SQLite is fine for local development and short-lived paper runs.
- For persistent paper operations, use a real Postgres URL in `DATABASE_URL`.
- If you keep SQLite in a containerized deployment, point it at persistent storage such as `sqlite:///./data/trading.db`.

## 6. Local quickstart

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Update `.env` with your local values:

- keep the paper-safe defaults for the first boot
- add Alpaca paper credentials only in `.env`
- set `API_ADMIN_TOKEN` if you want access to `/config`, `/diagnostics/*`, or `/admin/*`
- leave Discord disabled until you intentionally want alerts

Run the preferred single-process runtime:

```bash
source .venv/bin/activate
python scripts/run_paper_api.py --host 127.0.0.1 --port 8000
```

First verification commands:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/auto/status
curl -X POST http://127.0.0.1:8000/run-once \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","asset_class":"equity"}'
```

Protected route example:

```bash
curl http://127.0.0.1:8000/config \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
```

## 7. Recommended EC2/systemd or compose-based paper deployment

Recommended shape:

- Keep a single API/trader process.
- Prefer systemd for restart ownership on EC2.
- Use `scripts/run_paper_api.py` instead of multi-worker app servers.

Systemd path:

- Example service: `deploy/systemd/money-api.service`
- Example env file: `deploy/env/money.env.example`
- Recommended setup: copy the service to `/etc/systemd/system/`, place a real env file outside the repo, and set `API_ADMIN_TOKEN`, paper Alpaca credentials, and a production `DATABASE_URL`.

Compose path:

- `docker-compose.yml` is development-only. It bind-mounts the repo and forces safe local defaults.
- `docker-compose.prod.yml` is the production-oriented paper deployment shape.
- The production compose file does not bind-mount the repository, uses `env_file`-driven configuration, and preserves the single-process runtime.

Compose example:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

For production compose, set one of these in `.env` before starting:

- `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/money`
- `DATABASE_URL=sqlite:///./data/trading.db`

## 8. Admin API security expectations

- `/health` remains public for health checks and container probes.
- `/config`, `/diagnostics/*`, `/admin/reset-local-state`, and `/admin/notifications/test` require admin authentication.
- Authentication accepts either:
  - `Authorization: Bearer <token>`
  - `X-Admin-Token: <token>`
- `API_ADMIN_TOKEN` should be treated as required in any shared, remote, staging, or production environment.
- `/admin/notifications/test` remains development-only even when the request is authenticated.

## 9. Data/log storage expectations

- SQLite local/dev default: `sqlite:///./trading.db`
- Production recommendation: Postgres via `DATABASE_URL`
- Structured logs and artifacts live under `LOG_DIR`, including:
  - `app.jsonl`
  - `signals.jsonl`
  - `orders.jsonl`
  - `outcomes.jsonl`
  - `news_features.jsonl`
- Discord replay suppression state lives at `BROKER_ORDER_STATUS_CACHE_PATH`.
- If you run with SQLite in compose or on a VM, persist the SQLite path, `logs/`, and `models/`.

## 10. Known limitations

- The repository is Postgres-ready in configuration and documentation, but this PR does not migrate the SQLAlchemy layer or schemas away from SQLite-specific expectations.
- Admin auth is token-based and intentionally simple; there is no user management layer.
- Discord is the only supported alert channel.
- ML, news, and RL are optional subsystems and are not required for safe paper operation.
- The preferred runtime is single-process only; do not introduce multiple API workers for the in-process auto-trader.

## 11. Verification commands

Targeted tests first:

```bash
source .venv/bin/activate
uv run pytest tests/test_config.py tests/test_api.py
```

API startup sanity check:

```bash
source .venv/bin/activate
python scripts/run_paper_api.py --host 127.0.0.1 --port 8000
```

Runtime verification:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/auto/status
curl http://127.0.0.1:8000/config \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
curl -X POST http://127.0.0.1:8000/auto/run-now
curl -X POST http://127.0.0.1:8000/run-once \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","asset_class":"equity"}'
```
