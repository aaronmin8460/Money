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
- Alpaca can be used as the broker while market data routes through free providers by asset class. The default composite route is Yahoo Finance for equities/ETFs/options and CoinGecko for crypto.
- Live trading requires explicit acknowledgement through both `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_ACK=ENABLE_LIVE_TRADING`.
- ML can assist ranking and filtering, but it must not bypass hard stops, drawdown limits, or emergency exits.
- News features can enrich signals, but they never place orders directly.
- RL code remains offline and must not connect to paper or live execution.
- Runtime safety can place the bot into an explicit halted state when configured circuit breakers trip or an operator halts it manually.
- Halted mode blocks new exposure-increasing entries but still allows risk-reducing exits and cleanup.
- Startup and runtime reconciliation compare broker positions, tracked local positions, and tranche state. Material mismatches are surfaced explicitly, alert through Discord when enabled, and can halt new entries.

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
- `MARKET_DATA_PROVIDER_DEFAULT=composite` for broker-independent market data; see `docs/market_data_providers.md` for routing and cache/rate-limit settings

Required only when the related feature is enabled:

- `DISCORD_WEBHOOK_URL` when `DISCORD_NOTIFICATIONS_ENABLED=true`
- `OPENAI_API_KEY` when both `NEWS_FEATURES_ENABLED=true` and `NEWS_LLM_ENABLED=true`
- `LIVE_TRADING_ACK=ENABLE_LIVE_TRADING` when `LIVE_TRADING_ENABLED=true`

Database guidance:

- SQLite is fine for local development and short-lived paper runs.
- For persistent paper operations, use a real Postgres URL such as `postgresql+psycopg://USER:PASSWORD@HOST:5432/money`.
- If you keep SQLite in a containerized deployment, point it at persistent storage such as `sqlite:///./data/trading.db`.
- Schema changes are managed through Alembic. Use `alembic upgrade head` before startup and `alembic revision --autogenerate -m "..."` for follow-up revisions.

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
- keep the free market-data defaults unless you intentionally want Alpaca market data
- set `API_ADMIN_TOKEN` if you want access to `/config`, `/diagnostics/*`, or `/admin/*`
- leave Discord disabled until you intentionally want alerts

Initialize or update the schema after `DATABASE_URL` is set:

```bash
source .venv/bin/activate
alembic upgrade head
```

Run the preferred single-process runtime:

```bash
source .venv/bin/activate
python scripts/run_paper_api.py --host 127.0.0.1 --port 8000
```

First verification commands:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready
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

Runtime safety examples:

```bash
curl http://127.0.0.1:8000/diagnostics/runtime-safety \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
curl http://127.0.0.1:8000/diagnostics/reconciliation \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
curl -X POST http://127.0.0.1:8000/admin/runtime-safety/halt \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"note":"operator halt"}'
curl -X POST http://127.0.0.1:8000/admin/runtime-safety/resume \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"note":"resume after review","reset_consecutive_losing_exits":true}'
```

## 6A. Phase 5: Aggressive Paper, Diversified News, And Rate Limiting

Phase 5 adds three explicit operator-facing features:

- `TRADING_PROFILE=conservative|balanced|aggressive` so aggressive behavior is opt-in instead of replacing the safe default.
- Multi-source news ingestion with source attribution, Benzinga RSS support, and SEC filing/disclosure feeds that stay feature-only.
- FastAPI rate limiting with `slowapi`, including stricter limits for scanner/admin paths and a structured 429 response.

Aggressive paper mode stays paper-first:

- Keep `BROKER_MODE=paper`
- Keep `LIVE_TRADING_ENABLED=false`
- Enable aggressive behavior only after conservative paper verification is clean
- Hard risk controls, kill switches, drawdown limits, and emergency exits still win over aggressive entries

Example aggressive paper profile:

```bash
cat <<'EOF' >> .env
TRADING_PROFILE=aggressive
AGGRESSIVE_MODE_ENABLED=true
AGGRESSIVE_PROFILE_VERSION=v1
AGGRESSIVE_SHORTS_ENABLED=true
AGGRESSIVE_EXTENDED_HOURS_ENABLED=true
TRADING_ENABLED=true
AUTO_TRADE_ENABLED=true
LIVE_TRADING_ENABLED=false
EOF
```

Example diversified news configuration:

```bash
cat <<'EOF' >> .env
NEWS_FEATURES_ENABLED=true
NEWS_RSS_ENABLED=true
NEWS_SOURCE_IDS=default_rss,benzinga,sec
BENZINGA_RSS_ENABLED=true
BENZINGA_RSS_URLS=["https://www.benzinga.com/feeds/news"]
SEC_RSS_ENABLED=true
SEC_RSS_URLS=["https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom&owner=include&count=40"]
SEC_USER_AGENT=MoneyBot/1.0 (paper-safe research; contact=ops@example.com)
NEWS_LLM_ENABLED=false
EOF
```

To enable the OpenAI-assisted path, set `NEWS_LLM_ENABLED=true` and `OPENAI_API_KEY=<real key>`. To stay fully heuristic and offline-safe, keep `NEWS_LLM_ENABLED=false`. In both cases, the news pipeline remains feature-only and the fetch script continues safely on per-source or LLM failures.

Example rate-limit configuration:

```bash
cat <<'EOF' >> .env
RATE_LIMIT_ENABLED=true
RATE_LIMIT_DEFAULT=60/minute
RATE_LIMIT_STORAGE_URI=memory://
RATE_LIMIT_HEADERS_ENABLED=true
RATE_LIMIT_SCANNER=6/minute
RATE_LIMIT_ADMIN=5/minute
RATE_LIMIT_MARKET=30/minute
RATE_LIMIT_SIGNALS=20/minute
RATE_LIMIT_HEALTH_EXEMPT=true
EOF
```

Fetch multi-source news features locally:

```bash
source .venv/bin/activate
python scripts/fetch_news_features.py
tail -n 20 logs/news_features.jsonl
```

Verify aggressive profile, enabled news sources, LLM status, and rate limiting:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/auto/status
curl http://127.0.0.1:8000/config \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
```

Useful fields to look for:

- `/health`: `trading_profile`, `enabled_news_sources`, `news_llm_status`, `rate_limit_enabled`
- `/auto/status`: `trading_profile`, `trading_profile_summary`, `candidate_strategy_routing`
- `/config`: effective aggressive overrides, source URLs, and route-specific rate-limit values

Test rate limiting locally:

```bash
for i in 1 2 3; do
  curl -i "http://127.0.0.1:8000/scanner/opportunities?limit=1"
done
curl -i http://127.0.0.1:8000/health
```

The scanner endpoint should return `429` after the configured threshold, while `/health` should stay easy to poll when `RATE_LIMIT_HEALTH_EXEMPT=true`.

## 7. EC2/systemd paper deployment

Recommended shape:

- Keep one API process and one in-process auto-trader loop.
- Use `scripts/run_paper_api.py` as the systemd entrypoint.
- Let systemd own restart behavior.
- Keep `BROKER_MODE=paper` and `LIVE_TRADING_ENABLED=false`.

### Bootstrap the host

The bootstrap script installs Ubuntu packages, creates the venv, seeds `/etc/money/money.env`, and installs the systemd unit templates:

```bash
sudo mkdir -p /opt/money
sudo chown "$USER:$USER" /opt/money
git clone https://github.com/aaronmin8460/Money.git /opt/money
cd /opt/money
bash deploy/ec2/bootstrap.sh
```

### Edit the deployment env

Bootstrap installs a conservative starter env file at `/etc/money/money.env`. Edit that file before starting services:

```bash
sudoedit /etc/money/money.env
```

For the first boot, keep these safe values:

- `TRADING_ENABLED=false`
- `AUTO_TRADE_ENABLED=false`
- `LIVE_TRADING_ENABLED=false`
- `DISCORD_NOTIFICATIONS_ENABLED=false`
- `NEWS_FEATURES_ENABLED=false`
- `ML_RETRAIN_ENABLED=false`

Minimum values to fill in for a real EC2 paper deployment:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `API_ADMIN_TOKEN`
- `DATABASE_URL`

When you are ready for continuous paper order submission, change only these two lines:

- `TRADING_ENABLED=true`
- `AUTO_TRADE_ENABLED=true`

### Install or refresh the unit files

Bootstrap already installs the default units, but these are the exact refresh commands if you want to re-install them explicitly:

```bash
cd /opt/money
sudo install -m 0644 deploy/systemd/money-api.service /etc/systemd/system/money-api.service
sudo install -m 0644 deploy/systemd/money-news.service /etc/systemd/system/money-news.service
sudo install -m 0644 deploy/systemd/money-news.timer /etc/systemd/system/money-news.timer
sudo install -m 0644 deploy/systemd/money-retrain.service /etc/systemd/system/money-retrain.service
sudo install -m 0644 deploy/systemd/money-retrain.timer /etc/systemd/system/money-retrain.timer
sudo systemctl daemon-reload
```

### Enable and start the API plus timers

```bash
sudo systemctl enable --now money-api.service
sudo systemctl enable --now money-news.timer
sudo systemctl enable --now money-retrain.timer
```

### Verify the service and timers

Exact status commands:

```bash
sudo systemctl status money-api.service --no-pager
sudo systemctl status money-news.timer --no-pager
sudo systemctl status money-retrain.timer --no-pager
sudo systemctl list-timers --all 'money-*'
```

Exact journal commands:

```bash
sudo journalctl -u money-api.service -n 100 --no-pager
sudo journalctl -u money-news.service -n 100 --no-pager
sudo journalctl -u money-retrain.service -n 100 --no-pager
```

Exact API checks:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/auto/status
curl http://127.0.0.1:8000/config \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
```

Run the full operational verifier:

```bash
cd /opt/money
.venv/bin/python scripts/verify_phase4.py \
  --env-file /etc/money/money.env \
  --app-dir /opt/money \
  --base-url http://127.0.0.1:8000
```

### Verify the auto-trader really started

After you intentionally set `AUTO_TRADE_ENABLED=true`, confirm all three signals:

```bash
curl http://127.0.0.1:8000/auto/status
sudo journalctl -u money-api.service -n 150 --no-pager
```

What to look for:

- `/auto/status` should report `"enabled": true`, `"running": true`, and `"process_lock_acquired": true`.
- `money-api.service` logs should show the single-process entrypoint, startup configuration, the auto-trader start attempt, and whether the loop lock was acquired.
- `order_submission_mode` should read `dry_run` for safe first boot or `paper_order_submission` when paper order submission is intentionally enabled.

### Verify hourly news refresh and whether LLM or heuristics ran

Trigger the service manually once if you want an immediate check:

```bash
sudo systemctl start money-news.service
sudo journalctl -u money-news.service -n 100 --no-pager
tail -n 20 /opt/money/logs/news_features.jsonl
```

What to look for:

- `analysis_mode=llm` with `analysis_reason=llm_success` means the OpenAI path ran.
- `analysis_mode=heuristic` with reasons such as `news_llm_disabled`, `openai_api_key_missing`, or `llm_fallback_after_error` means the pipeline degraded safely without crashing.
- The timer should remain active even if LLM is disabled; RSS-only refresh is still valid feature generation.

### Verify nightly retrain behavior

Trigger it manually and inspect the journal:

```bash
sudo systemctl start money-retrain.service
sudo journalctl -u money-retrain.service -n 150 --no-pager
```

What to look for:

- `nightly_retrain_skipped=true reason=ml_retrain_disabled` means the timer path is healthy but intentionally disabled.
- `nightly_retrain_skipped=true reason=no_training_rows` or `reason=no_models_trained` means sparse data skipped cleanly with exit code `0`.
- Successful runs log each step and use the project venv explicitly through `/opt/money/.venv/bin/python`.

### Deploy updates safely later

```bash
cd /opt/money
bash deploy/ec2/pull_and_restart.sh
```

That update script does the following:

- `git pull --ff-only`
- refreshes Python dependencies in the project venv
- reloads systemd if unit files changed
- restarts `money-api.service`
- restarts the news/retrain timers only if they were already enabled or active
- prints a short post-deploy health summary

Compose files remain in the repository for non-EC2 workflows, but the Phase 4 path is systemd-first.

## 8. Admin API security expectations

- `/health` remains public for health checks and container probes.
- `/health/ready` is the public readiness probe and verifies runtime configuration plus database connectivity without placing trades.
- `/config`, `/diagnostics/*`, `/admin/reset-local-state`, and `/admin/notifications/test` require admin authentication.
- `/admin/runtime-safety/halt` and `/admin/runtime-safety/resume` are protected admin controls for intentional halt and recovery actions.
- Authentication accepts either:
  - `Authorization: Bearer <token>`
  - `X-Admin-Token: <token>`
- `API_ADMIN_TOKEN` should be treated as required in any shared, remote, staging, or production environment.
- `/admin/notifications/test` remains development-only even when the request is authenticated.

### Runtime safety operations

- `HALT_ON_CONSECUTIVE_LOSSES=true` with `MAX_CONSECUTIVE_LOSING_EXITS=3` halts new entries after the configured number of realized losing exits in a row.
- `HALT_ON_RECONCILE_MISMATCH=true` can halt new entries when reconciliation finds material broker/local state drift.
- `HALT_ON_STARTUP_SYNC_FAILURE=true` can halt new entries if startup synchronization fails.
- `/diagnostics/runtime-safety` shows the halted flag, halt reason and rule, consecutive loss count, entry allowance, loop metadata, and lock metadata.
- `/diagnostics/reconciliation` shows the last reconcile status, mismatch summary, tracked local positions, broker positions, and tranche-state context.
- Manual resume intentionally clears the halt. By default it also resets the consecutive losing exit counter unless you send `{"reset_consecutive_losing_exits": false}`.
- Discord remains the only supported alert channel for halt, resume, reconcile mismatch, startup sync failure, and auto-heal events.

## 9. Data/log storage expectations

- SQLite local/dev default: `sqlite:///./trading.db`
- Production recommendation: Postgres via `DATABASE_URL`
- Alembic migration state is stored in the database via the `alembic_version` table.
- Structured logs and artifacts live under `LOG_DIR`, including:
  - `app.jsonl`
  - `signals.jsonl`
  - `orders.jsonl`
  - `outcomes.jsonl`
  - `news_features.jsonl`
- Discord replay suppression state lives at `BROKER_ORDER_STATUS_CACHE_PATH`.
- If you run with SQLite in compose or on a VM, persist the SQLite path, `logs/`, and `models/`.

## 10. Known limitations

- Admin auth is token-based and intentionally simple; there is no user management layer.
- Discord is the only supported alert channel.
- ML, news, and RL are optional subsystems and are not required for safe paper operation.
- The preferred runtime is single-process only; do not introduce multiple API workers for the in-process auto-trader.

## 11. Verification commands

Targeted tests first:

```bash
source .venv/bin/activate
pytest tests/test_config.py tests/test_db_session.py tests/test_api.py tests/test_alembic.py tests/test_phase4_ops.py
```

API startup sanity check:

```bash
source .venv/bin/activate
python scripts/run_paper_api.py --host 127.0.0.1 --port 8000
```

Runtime verification:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/auto/status
curl http://127.0.0.1:8000/diagnostics/runtime-safety \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
curl http://127.0.0.1:8000/diagnostics/reconciliation \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
curl http://127.0.0.1:8000/config \
  -H "Authorization: Bearer ${API_ADMIN_TOKEN}"
curl -X POST http://127.0.0.1:8000/auto/run-now
curl -X POST http://127.0.0.1:8000/run-once \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","asset_class":"equity"}'
```

Phase 4 EC2 verification:

```bash
cd /opt/money
.venv/bin/python scripts/verify_phase4.py \
  --env-file /etc/money/money.env \
  --app-dir /opt/money \
  --base-url http://127.0.0.1:8000
sudo systemctl status money-api.service --no-pager
sudo systemctl list-timers --all 'money-*'
sudo journalctl -u money-api.service -n 100 --no-pager
sudo journalctl -u money-news.service -n 100 --no-pager
sudo journalctl -u money-retrain.service -n 100 --no-pager
```
