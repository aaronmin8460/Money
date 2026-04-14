# Operations Runbook

## 1. Environment file location

- Keep the real production env file outside the repo.
- Recommended host path for EC2 or other long-lived Linux hosts: `/etc/money/money.env`.
- For Compose, point `MONEY_ENV_FILE` at that same file.
- Use `.env.example` and `deploy/env/money.env.example` only as templates. Do not store real secrets in tracked files.

## 2. Install dependencies

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure paper-safe environment values

- Keep `BROKER_MODE=paper`.
- Keep `LIVE_TRADING_ENABLED=false`.
- Keep `TRADING_ENABLED=false` and `AUTO_TRADE_ENABLED=false` until health, readiness, and broker checks pass.
- Use either:
  - `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/money` for persistent paper trading
  - `DATABASE_URL=sqlite:///./data/trading.db` for persistent local or single-host SQLite
- Set `API_ADMIN_TOKEN` for any shared or remote environment.
- Enable Discord only when you have a real `DISCORD_WEBHOOK_URL`.

## 4. Run database migrations

```bash
source .venv/bin/activate
set -a
source /etc/money/money.env
set +a
alembic upgrade head
```

- Application startup also applies `alembic upgrade head`, but run the command explicitly during deployment so migration failures show up before you cut traffic over.
- For future schema work:

```bash
source .venv/bin/activate
alembic revision --autogenerate -m "describe change"
```

## 5. Start the app

Direct process:

```bash
source .venv/bin/activate
python scripts/run_paper_api.py --host 0.0.0.0 --port 8000
```

Compose:

```bash
MONEY_ENV_FILE=/etc/money/money.env docker compose -f docker-compose.prod.yml up -d --build
```

Systemd:

- Install `deploy/systemd/money-api.service`.
- Set `EnvironmentFile=/etc/money/money.env`.
- Use systemd for restart ownership.

## 6. Verify liveness and readiness

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready
```

- `/health` is the lightweight public liveness check.
- `/health/ready` verifies runtime configuration loading and database connectivity. It does not place trades.

## 7. Confirm paper mode is active

- Check the env file:
  - `BROKER_MODE=paper`
  - `LIVE_TRADING_ENABLED=false`
- Check runtime status:

```bash
curl http://127.0.0.1:8000/broker/status
curl http://127.0.0.1:8000/config -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
```

- Confirm `broker_mode` is `paper`.
- Confirm `live_trading_enabled` is `false`.
- Confirm `trading_enabled` and `auto_trade_enabled` match the rollout stage you intend.

## 8. Confirm Discord notifications

- If Discord is disabled, keep `DISCORD_NOTIFICATIONS_ENABLED=false` and `DISCORD_WEBHOOK_URL=` blank.
- If Discord is enabled:
  - confirm `DISCORD_NOTIFICATIONS_ENABLED=true`
  - confirm `DISCORD_WEBHOOK_URL` is a real webhook in the host env file
  - confirm the protected `/config` route reports Discord enabled
  - after a controlled restart, confirm the expected startup notification arrives in Discord
- The development-only `/admin/notifications/test` endpoint must not be used as a production probe.

## 9. Persisted paths and storage

- Persist the database itself:
  - Postgres data on the database host or service
  - SQLite file path if you use SQLite, usually under `data/`
- Persist application state directories:
  - `logs/`
  - `models/`
  - `data/` when SQLite or local CSV inputs are used
- Persist the broker replay-suppression file at `BROKER_ORDER_STATUS_CACHE_PATH`.

## 10. Checks after restart

- `curl http://127.0.0.1:8000/health`
- `curl http://127.0.0.1:8000/health/ready`
- `curl http://127.0.0.1:8000/auto/status`
- Review the latest app logs in `LOG_DIR`.
- Confirm the database path or Postgres connection is unchanged.
- Confirm the `alembic_version` table is present in the database.
- Confirm the auto-trader state matches expectation before enabling automated paper trading.

## 11. Checks before guarded live mode

- Re-confirm this repo defaults to paper-safe operation and guarded live mode is not the default.
- Review database backups or rollback posture.
- Confirm `API_ADMIN_TOKEN` is set and protected.
- Confirm Discord alerts are working for startup, errors, and rejections.
- Confirm `LIVE_TRADING_ENABLED=false` is still in place until you intentionally switch modes.
- Before any guarded live activation, verify the required acknowledgement:
  - `LIVE_TRADING_ENABLED=true`
  - `LIVE_TRADING_ACK=ENABLE_LIVE_TRADING`
- Re-check risk limits, notional caps, drawdown limits, and hard exits before any live rollout.
