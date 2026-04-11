#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/money}"

cd "${APP_DIR}"
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart money-api.service
