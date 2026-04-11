# Money Trading Bot

FastAPI-based paper-trading platform for scanning, risk-filtering, paper execution, structured logging, Discord notifications, ML scoring, and offline research workflows.

The default posture is paper-safe:

- `BROKER_MODE=paper`
- `TRADING_ENABLED=false`
- `AUTO_TRADE_ENABLED=false`
- `DISCORD_NOTIFICATIONS_ENABLED=false`
- `ML_ENABLED=false`
- `ML_RETRAIN_ENABLED=false`
- `NEWS_FEATURES_ENABLED=false`
- `ALLOW_EXTENDED_HOURS=false`

## V1 Architecture

`scanner -> strategy -> risk context -> ML score filter -> execution -> broker response -> logs/Discord -> dataset export -> retrain/evaluate/promote loop`

Important guardrails:

- news is feature-only and never places orders directly
- RL is sandbox-only and never touches the live or paper execution path
- ML is a filter/booster only and never overrides hard risk controls
- the preferred local/API runtime stays single-process through `scripts/run_paper_api.py`

## Repository Layout

- `app/api/`: FastAPI routes, diagnostics, admin helpers
- `app/config/`: environment-backed settings
- `app/db/`: SQLAlchemy models and schema init
- `app/domain/`: normalized market and asset models
- `app/execution/`: signal-to-order flow and persistence
- `app/monitoring/`: app logging, Discord notifications, JSONL artifact writers
- `app/ml/`: feature schema, inference, training, evaluation, registry helpers
- `app/news/`: RSS ingestion, conservative ticker mapping, optional LLM analysis, feature store
- `app/rl/`: experimental replay-only RL sandbox
- `app/services/`: auto-trader, broker, scanner, market data, runtime wiring
- `deploy/`: EC2 bootstrap scripts, env example, systemd units/timers
- `scripts/`: local run path, model ops, news ingestion, RL experiment
- `models/`: current/candidate model artifacts and `registry.json`
- `logs/`: JSONL signal/order/outcome/news artifacts plus `app.jsonl`

## Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Guaranteed baseline ML dependencies are in `requirements.txt`:

- `scikit-learn`
- `joblib`
- `feedparser`
- `openai`

`xgboost` is supported when installed, but it is intentionally optional.

## Local Run Commands

Recommended single-process API run path:

```bash
source .venv/bin/activate
python scripts/run_paper_api.py --host 127.0.0.1 --port 8000
```

Manual one-off symbol evaluation:

```bash
source .venv/bin/activate
python scripts/run_once.py
```

Exact local test commands:

```bash
source .venv/bin/activate
uv run pytest tests/test_config.py tests/test_discord_notifications.py tests/test_auto_trader.py tests/test_execution_persistence.py tests/test_v1_platform.py tests/test_api.py
uv run pytest
```

## Phase A: Logging And Discord

### Logging Artifacts

The app now writes structured JSONL artifacts under `LOG_DIR`:

- `logs/app.jsonl`
- `logs/signals.jsonl`
- `logs/orders.jsonl`
- `logs/outcomes.jsonl`
- `logs/news_features.jsonl`

The directory is created automatically.

### What Gets Logged

- every executed or evaluated signal that reaches structured persistence
- order proposals and broker-facing submission details
- normalized outcome classifications for later ML export
- optional news feature rows
- structured app logs with contextual metadata

Outcome classifications include:

- `market_closed`
- `market_closed_extended_hours_disabled`
- `extended_hours_not_supported_for_asset`
- `no_position_to_sell`
- `risk_rejected`
- `dry_run`
- `submitted`
- `skipped_low_ml_score`

### Discord Behavior

Discord is intentionally meaningful instead of noisy.

Sent when enabled:

- order submitted
- dry-run order only when `DISCORD_NOTIFY_DRY_RUNS=true`
- risk rejection when `DISCORD_NOTIFY_REJECTIONS=true`
- startup / shutdown
- auto-trader cycle error
- optional compact scan summary when `DISCORD_NOTIFY_SCAN_SUMMARY=true`

Not sent:

- `HOLD`
- noisy heartbeats
- giant raw dict dumps

Dedupe protection exists for:

- scan summaries
- broker lifecycle updates
- startup/shutdown duplicates across quick reloads via a short-lived local dedupe cache

## Signal, Trade, And Outcome Storage

Signal/order/outcome storage now has two layers:

1. SQLite persistence for normalized signals, orders, fills, positions, and bot runs
2. JSONL artifact logs for downstream ML/news/retrain workflows

`logs/outcomes.jsonl` is the main bootstrap source for model export. Each row carries:

- signal identity
- cycle id
- action and classification
- risk rule and reason
- feature snapshot
- optional ML score metadata
- optional news feature metadata

## ML Scoring

### Where ML Sits

ML scoring is optional and disabled by default.

- strategies still generate the primary signal
- ML only scores `BUY` candidates
- if the score is below `ML_MIN_SCORE_THRESHOLD`, the candidate becomes `skipped_low_ml_score`
- risk controls still run independently and remain authoritative

### Model Types

- guaranteed baseline: `logistic_regression`
- optional if installed: `xgboost`

### Runtime Artifacts

- `models/current_model.joblib`
- `models/candidate_model.joblib`
- `models/registry.json`

### Model Registry

`models/registry.json` tracks:

- `current_model`
- `candidate_model`
- `created_at`
- `promoted`
- `model_type`
- `feature_version`
- `train_rows`
- `validation_rows`
- `metrics`
- `notes`

Helper functions live in `app/ml/registry.py` for initialize/load/update/promote/rollback flows.

## Training, Evaluation, And Promotion

### Export Training Data

```bash
source .venv/bin/activate
python scripts/export_training_data.py --output models/training_data.jsonl
```

### Train Candidate Model

```bash
source .venv/bin/activate
python scripts/train_model.py --dataset models/training_data.jsonl
```

### Evaluate Current And Candidate

```bash
source .venv/bin/activate
python scripts/evaluate_model.py --dataset models/training_data.jsonl
```

### Promote Candidate

```bash
source .venv/bin/activate
python scripts/promote_model.py
```

### Nightly Retrain Wrapper

```bash
source .venv/bin/activate
ML_RETRAIN_ENABLED=true bash scripts/run_nightly_retrain.sh
```

Promotion is threshold-gated by:

- `ML_PROMOTION_MIN_AUC`
- `ML_PROMOTION_MIN_PRECISION`
- `ML_PROMOTION_MIN_WINRATE_LIFT`

Sparse data is handled safely. If there are not enough labeled rows, the scripts log a skip instead of crashing the system.

### Label Bootstrapping Note

This v1 uses conservative bootstrap labels from structured outcome data. When realized trade outcome history is sparse, the export path falls back to execution outcome plus basic reward/risk heuristics. That keeps the retrain loop operational without pretending the labels are a full production-grade realized-PnL dataset yet.

## News Feature Pipeline

### What It Does

- ingests RSS headlines
- maps headlines to configured symbols conservatively
- groups headlines per ticker and time window
- optionally calls an OpenAI model for summary/sentiment/risk tagging
- stores the result as features only

### What It Does Not Do

- it does not place orders
- it does not bypass strategy logic
- it does not override risk controls

### Fetch News Features

```bash
source .venv/bin/activate
python scripts/fetch_news_features.py
python scripts/fetch_news_features.py --symbols AAPL MSFT BTC/USD
```

If `OPENAI_API_KEY` is missing, the code falls back to a heuristic non-LLM analysis path and continues safely.

Relevant env vars:

- `NEWS_FEATURES_ENABLED`
- `NEWS_RSS_ENABLED`
- `NEWS_LLM_ENABLED`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `NEWS_MAX_HEADLINES_PER_TICKER`
- `NEWS_LOOKBACK_HOURS`

## Diagnostics And Verification

Core runtime/status routes:

- `GET /auto/status`
- `POST /auto/start`
- `POST /auto/stop`
- `POST /auto/run-now`
- `POST /run-once`

Diagnostics routes:

- `GET /diagnostics/auto`
- `GET /diagnostics/risk`
- `GET /diagnostics/strategy`
- `GET /diagnostics/portfolio`
- `GET /diagnostics/tranches`
- `GET /diagnostics/rejections/latest`

Exact curl commands:

```bash
curl http://127.0.0.1:8000/auto/status
curl -X POST http://127.0.0.1:8000/auto/run-now
curl -X POST http://127.0.0.1:8000/run-once -H "Content-Type: application/json" -d '{"symbol":"AAPL","asset_class":"equity"}'
curl http://127.0.0.1:8000/diagnostics/auto
curl http://127.0.0.1:8000/diagnostics/risk
curl http://127.0.0.1:8000/diagnostics/strategy
curl http://127.0.0.1:8000/diagnostics/portfolio
curl http://127.0.0.1:8000/diagnostics/tranches
curl http://127.0.0.1:8000/diagnostics/rejections/latest
```

To verify only one auto-trader loop is active:

- run the API through `python scripts/run_paper_api.py`
- check `GET /auto/status` for `running`, `thread_ident`, `process_lock_acquired`, and `auto_trader_lock_path`
- on EC2, use `systemctl status money-api` and `journalctl -u money-api -n 100 --no-pager`

## AWS EC2 Quickstart

Assumptions:

- Ubuntu host
- venv-based deployment first
- internal API/bot usage
- no nginx required

Bootstrap:

```bash
chmod +x deploy/ec2/bootstrap.sh
APP_DIR=/opt/money bash deploy/ec2/bootstrap.sh
sudo mkdir -p /etc/money
sudo cp deploy/env/money.env.example /etc/money/money.env
sudo nano /etc/money/money.env
```

Install systemd units:

```bash
sudo cp deploy/systemd/money-api.service /etc/systemd/system/
sudo cp deploy/systemd/money-retrain.service /etc/systemd/system/
sudo cp deploy/systemd/money-retrain.timer /etc/systemd/system/
sudo cp deploy/systemd/money-news.service /etc/systemd/system/
sudo cp deploy/systemd/money-news.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable money-api.service
sudo systemctl enable money-retrain.timer
sudo systemctl enable money-news.timer
sudo systemctl start money-api.service
sudo systemctl start money-retrain.timer
sudo systemctl start money-news.timer
```

Deploy update:

```bash
chmod +x deploy/ec2/pull_and_restart.sh
APP_DIR=/opt/money bash deploy/ec2/pull_and_restart.sh
```

Useful journal commands:

```bash
journalctl -u money-api -f
journalctl -u money-api -n 200 --no-pager
journalctl -u money-retrain.service -n 200 --no-pager
journalctl -u money-news.service -n 200 --no-pager
systemctl status money-api
systemctl list-timers --all | grep money
```

## systemd Behavior

`deploy/systemd/money-api.service` is configured for:

- `Restart=always`
- single-process `scripts/run_paper_api.py`
- environment loading via `EnvironmentFile`
- correct `WorkingDirectory`

That preserves the single in-process auto-trader loop and makes systemd the owner of restart behavior.

## RL Sandbox Disclaimer

`app/rl/` and `scripts/rl_experiment.py` are experimental only.

- they use replay/offline simulation concepts only
- they are not wired into paper execution
- they are not wired into live execution

Try the stub:

```bash
source .venv/bin/activate
python scripts/rl_experiment.py
```

## Key Env Vars

Core:

- `BROKER_MODE`
- `TRADING_ENABLED`
- `AUTO_TRADE_ENABLED`
- `ACTIVE_STRATEGY`
- `ALLOW_EXTENDED_HOURS`
- `LOG_DIR`
- `AUTO_TRADER_LOCK_PATH`

Discord:

- `DISCORD_NOTIFICATIONS_ENABLED`
- `DISCORD_WEBHOOK_URL`
- `DISCORD_NOTIFY_DRY_RUNS`
- `DISCORD_NOTIFY_REJECTIONS`
- `DISCORD_NOTIFY_ERRORS`
- `DISCORD_NOTIFY_START_STOP`
- `DISCORD_NOTIFY_SCAN_SUMMARY`

ML:

- `ML_ENABLED`
- `ML_MODEL_TYPE`
- `ML_MIN_SCORE_THRESHOLD`
- `ML_MIN_TRAIN_ROWS`
- `ML_RETRAIN_ENABLED`
- `ML_PROMOTION_MIN_AUC`
- `ML_PROMOTION_MIN_PRECISION`
- `ML_PROMOTION_MIN_WINRATE_LIFT`
- `ML_CURRENT_MODEL_PATH`
- `ML_CANDIDATE_MODEL_PATH`
- `ML_REGISTRY_PATH`

News:

- `NEWS_FEATURES_ENABLED`
- `NEWS_RSS_ENABLED`
- `NEWS_LLM_ENABLED`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `NEWS_MAX_HEADLINES_PER_TICKER`
- `NEWS_LOOKBACK_HOURS`

## Operational Notes

- use `scripts/run_paper_api.py` locally and in systemd to avoid duplicate worker processes
- keep `TRADING_ENABLED=false` until you explicitly want paper order submission
- ML/news/RL are additive and optional; the bot still runs safely with all of them disabled
- this repository does not claim live-trading readiness from the new ML, news, or RL additions
