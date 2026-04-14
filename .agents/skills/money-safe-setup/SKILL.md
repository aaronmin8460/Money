---
name: money-safe-setup
description: Set up or repair the Money trading bot safely for local development, paper trading, or EC2. Use when the user asks about installation, environment variables, Alpaca paper setup, Discord webhook setup, Python setup, startup commands, API boot, or first-run verification. Do not use for strategy redesigns.
---

# Money safe setup

Use this skill when the task is about bootstrapping, fixing, or validating the runtime environment for the Money repo.

## Goals

- Get the repo running locally or on EC2
- Keep the system paper-safe by default
- Prevent secret leakage and accidental live-trading activation
- Verify startup with concrete commands

## Workflow

1. Check whether the request is local setup, EC2 setup, env setup, or runtime repair.
2. Prefer paper-safe settings unless the user explicitly requests otherwise.
3. If `.env.example` or docs expose secrets or unsafe defaults, call that out and recommend sanitizing them.
4. Use the repo’s preferred single-process runtime path.
5. Provide verification commands after setup, not just install commands.

## Local setup baseline

- Create Python 3.12 virtual environment
- Activate venv
- Install `requirements.txt`
- Copy `.env.example` to `.env`
- Replace all secrets with real local values only in `.env`
- Keep:
  - `BROKER_MODE=paper`
  - `LIVE_TRADING_ENABLED=false`
  - `ML_ENABLED=false` unless the task is ML-specific
- Strongly prefer:
  - `TRADING_ENABLED=false`
  - `AUTO_TRADE_ENABLED=false`
  - `DISCORD_NOTIFICATIONS_ENABLED=false`
    for the very first boot

## First boot

Run:

- `python scripts/run_paper_api.py --host 127.0.0.1 --port 8000`

Then verify:

- `curl http://127.0.0.1:8000/auto/status`
- `curl -X POST http://127.0.0.1:8000/run-once -H "Content-Type: application/json" -d '{"symbol":"AAPL","asset_class":"equity"}'`

## EC2 setup

When the user asks about Ubuntu or EC2:

- prefer the repo’s bootstrap and systemd flow
- keep the API single-process
- keep restart behavior owned by systemd
- use the repo env file location and journalctl commands when relevant

## Do not

- Do not tell the user to put real secrets in `.env.example`
- Do not enable live trading by default
- Do not recommend multi-worker uvicorn for the main auto-trader runtime
- Do not skip verification
