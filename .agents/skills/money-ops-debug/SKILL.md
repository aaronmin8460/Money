---
name: money-ops-debug
description: Debug Money repo runtime, broker, Discord, API, or server issues. Use when the user asks why orders are rejected, why Discord alerts are duplicated or missing, why the bot is not trading, why startup fails, why systemd restarts fail, or how to inspect status, logs, and diagnostics.
---

# Money ops debug

Use this skill for troubleshooting the running system.

## Primary targets

- API startup failures
- Python or dependency issues
- Alpaca auth/config problems
- order rejection or no-trade behavior
- Discord notification issues
- duplicate broker lifecycle alerts
- EC2 systemd and restart issues

## Workflow

1. Determine whether the issue is config, runtime, broker, notification, or deployment.
2. Check environment assumptions first:
   - broker mode
   - trading flags
   - API keys present
   - Discord webhook present when notifications are expected
3. Use the repo’s diagnostics and status routes before proposing code changes.
4. If the issue involves duplicate broker lifecycle alerts, preserve startup replay suppression behavior.
5. For EC2 issues, prefer journalctl/systemctl evidence over guesses.

## Minimum debug checks

- `curl http://127.0.0.1:8000/auto/status`
- `curl http://127.0.0.1:8000/diagnostics/auto`
- `curl http://127.0.0.1:8000/diagnostics/risk`
- `curl http://127.0.0.1:8000/diagnostics/rejections/latest`

## Important reminders

- `scripts/run_paper_api.py` is the preferred runtime path for the in-process auto-trader
- avoid recommending multi-worker runtime for this repo
- secrets must never be pasted back into tracked files
- explain whether the problem is config, market conditions, risk logic, broker response, or code defect

## Output style

Return:

1. likely root cause
2. exact commands to prove it
3. exact files to inspect or patch
4. safe fix path
