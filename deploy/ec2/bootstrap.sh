#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/money}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

sudo apt-get update
sudo apt-get install -y "${PYTHON_BIN}" "${PYTHON_BIN}-venv" git

sudo mkdir -p "${APP_DIR}"
sudo chown -R "$USER":"$USER" "${APP_DIR}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  git clone "https://github.com/aaronmin8460/Money.git" "${APP_DIR}"
fi

cd "${APP_DIR}"
"${PYTHON_BIN}" -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

mkdir -p logs models

echo "Bootstrap complete. Copy deploy/env/money.env.example to /etc/money/money.env before enabling systemd."
