#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/money}"
ENV_DIR="${ENV_DIR:-/etc/money}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REPO_URL="${REPO_URL:-https://github.com/aaronmin8460/Money.git}"
APP_OWNER="${APP_OWNER:-${SUDO_USER:-$USER}}"
APP_GROUP="${APP_GROUP:-$APP_OWNER}"

log() {
  printf '[bootstrap] %s\n' "$*"
}

render_install() {
  local source_path="$1"
  local destination_path="$2"
  local rendered
  rendered="$(mktemp)"
  sed \
    -e "s|/opt/money|${APP_DIR}|g" \
    -e "s|/etc/money|${ENV_DIR}|g" \
    "${source_path}" > "${rendered}"
  sudo install -m 0644 "${rendered}" "${destination_path}"
  rm -f "${rendered}"
}

log "Installing Ubuntu packages required for the venv-based deployment."
sudo apt-get update
sudo apt-get install -y git curl ca-certificates python3 python3-venv python3-pip

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  log "Configured PYTHON_BIN is unavailable: ${PYTHON_BIN}"
  exit 1
fi

sudo install -d -m 0755 "${APP_DIR}" "${ENV_DIR}"
sudo chown -R "${APP_OWNER}:${APP_GROUP}" "${APP_DIR}"

if [[ -d "${APP_DIR}/.git" ]]; then
  log "Repository already exists at ${APP_DIR}; fetching the latest refs."
  git -C "${APP_DIR}" fetch --all --tags --prune
elif find "${APP_DIR}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  log "APP_DIR is not empty and is not a Money git checkout: ${APP_DIR}"
  exit 1
else
  log "Cloning ${REPO_URL} into ${APP_DIR}."
  git clone "${REPO_URL}" "${APP_DIR}"
fi

cd "${APP_DIR}"

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  log "Creating Python virtual environment with ${PYTHON_BIN}."
  "${PYTHON_BIN}" -m venv .venv
else
  log "Reusing existing virtual environment at ${APP_DIR}/.venv."
fi

log "Installing or refreshing Python dependencies."
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install -r requirements.txt

mkdir -p data logs models

if [[ ! -f "${ENV_DIR}/money.env" ]]; then
  log "Installing conservative starter env file at ${ENV_DIR}/money.env."
  sudo install -m 0640 deploy/env/money.env.example "${ENV_DIR}/money.env"
else
  log "Keeping existing runtime env file at ${ENV_DIR}/money.env."
fi
sudo install -m 0644 deploy/env/money.env.example "${ENV_DIR}/money.env.example"

log "Installing systemd unit templates."
for unit_path in deploy/systemd/*.service deploy/systemd/*.timer; do
  render_install "${unit_path}" "${SYSTEMD_DIR}/$(basename "${unit_path}")"
done
sudo systemctl daemon-reload

log "Bootstrap complete."
log "Next steps:"
log "  1. Edit ${ENV_DIR}/money.env with Alpaca paper credentials and deployment settings."
log "  2. Enable the API service: sudo systemctl enable --now money-api.service"
log "  3. Enable the timers: sudo systemctl enable --now money-news.timer money-retrain.timer"
