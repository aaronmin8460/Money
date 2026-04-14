# Money repository instructions

## Purpose

This repository is a FastAPI-based trading bot focused on paper-safe operation, structured logging, Discord notifications, ML-assisted scoring, and offline research workflows.

## Core rules

- Default to paper-safe behavior.
- Do not enable live trading unless the user explicitly asks for it and explicitly acknowledges the risk.
- Never commit real secrets, API keys, tokens, webhooks, or account identifiers.
- Treat `.env.example` as a placeholder-only file. It must never contain real credentials.
- When editing trading behavior, preserve hard risk controls. ML may assist ranking/filtering but must not bypass hard stops, drawdown limits, or emergency exits.
- News features are feature-only. They must not directly place orders.
- RL code is sandbox-only and must not be connected to live or paper execution.
- Preserve the preferred single-process runtime using `scripts/run_paper_api.py` unless the user explicitly asks for an architecture change.

## Repo map

- `app/api/`: FastAPI routes and diagnostics
- `app/config/`: settings and environment parsing
- `app/db/`: models and schema init
- `app/execution/`: order execution and persistence
- `app/monitoring/`: logs, Discord notifications, JSONL artifacts
- `app/ml/`: inference, training, evaluation, registry
- `app/news/`: RSS and optional LLM feature extraction
- `app/rl/`: experimental offline sandbox only
- `app/services/`: auto trader, broker, scanner, market data, runtime wiring
- `deploy/`: EC2 bootstrap, env examples, systemd units and timers
- `scripts/`: run paths, training/export/evaluation helpers
- `tests/`: regression and platform tests

## How to work in this repo

- Read the relevant service/module before changing behavior.
- Prefer focused, minimal changes over broad rewrites.
- Keep function and module responsibilities explicit.
- Preserve backward compatibility for paper trading unless the user explicitly wants a breaking redesign.
- When changing trading logic, also review logging, diagnostics, and persistence impact.
- When changing environment variables, update `.env.example` and README-relevant commands if needed.

## Safe defaults for environment changes

When creating or editing example env files, prefer:

- `BROKER_MODE=paper`
- `TRADING_ENABLED=false`
- `AUTO_TRADE_ENABLED=false`
- `LIVE_TRADING_ENABLED=false`
- `DISCORD_NOTIFICATIONS_ENABLED=false`
- `ML_ENABLED=false`
- `ML_RETRAIN_ENABLED=false`
- blank values for all secrets

## Verification expectations

After code changes, use the smallest relevant verification set first:

1. configuration/tests for touched modules
2. API startup check
3. targeted diagnostics route checks
4. broader test sweep only when needed

## Preferred local commands

- Create venv: `python3.12 -m venv .venv`
- Activate: `source .venv/bin/activate`
- Install deps: `pip install -r requirements.txt`
- Run API: `python scripts/run_paper_api.py --host 127.0.0.1 --port 8000`

## API/runtime checks

- `curl http://127.0.0.1:8000/auto/status`
- `curl -X POST http://127.0.0.1:8000/auto/run-now`
- `curl -X POST http://127.0.0.1:8000/run-once -H "Content-Type: application/json" -d '{"symbol":"AAPL","asset_class":"equity"}'`

## Change-specific guidance

### Trading strategy / execution

- Do not bypass risk checks.
- Preserve position sizing, partial exits, stop logic, and lifecycle logging.
- Check for side effects in Discord notifications and structured JSONL output.

### ML

- Keep ML optional.
- Ensure training/evaluation/promotion flows degrade safely when data is sparse.
- Do not silently promote weak candidate models.

### Deployment / ops

- Prefer systemd ownership of process restarts on EC2.
- Preserve single auto-trader loop behavior.
- Avoid introducing multi-worker API modes unless explicitly requested.

## Done means

A change is done only if:

- code is consistent with the repo’s paper-safe posture
- touched behavior is verified with relevant tests or API checks
- secrets are not exposed
- docs/env examples remain accurate
