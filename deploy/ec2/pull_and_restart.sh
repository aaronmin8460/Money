#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/money}"
ENV_DIR="${ENV_DIR:-/etc/money}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
BOOTSTRAP_PYTHON_BIN="${BOOTSTRAP_PYTHON_BIN:-python3}"
VENV_PYTHON="${VENV_PYTHON:-${APP_DIR}/.venv/bin/python}"

log() {
  printf '[deploy] %s\n' "$*"
}

render_unit_if_changed() {
  local source_path="$1"
  local destination_path="${SYSTEMD_DIR}/$(basename "${source_path}")"
  local rendered
  rendered="$(mktemp)"
  sed \
    -e "s|/opt/money|${APP_DIR}|g" \
    -e "s|/etc/money|${ENV_DIR}|g" \
    "${source_path}" > "${rendered}"
  if [[ ! -f "${destination_path}" ]] || ! cmp -s "${rendered}" "${destination_path}"; then
    sudo install -m 0644 "${rendered}" "${destination_path}"
    CHANGED_UNITS+=("$(basename "${source_path}")")
  fi
  rm -f "${rendered}"
}

restart_timer_if_enabled_or_active() {
  local timer_name="$1"
  if sudo systemctl is-enabled --quiet "${timer_name}" || sudo systemctl is-active --quiet "${timer_name}"; then
    sudo systemctl restart "${timer_name}"
    log "Restarted ${timer_name}."
  else
    log "${timer_name} is installed but not enabled; leaving it unchanged."
  fi
}

timer_next_run() {
  local timer_name="$1"
  sudo systemctl show "${timer_name}" --property=NextElapseUSecRealtime --value 2>/dev/null || true
}

cd "${APP_DIR}"

if [[ ! -d .git ]]; then
  log "APP_DIR is not a git checkout: ${APP_DIR}"
  exit 1
fi

PREVIOUS_HEAD="$(git rev-parse HEAD)"
log "Pulling the latest fast-forward changes."
git pull --ff-only
CURRENT_HEAD="$(git rev-parse HEAD)"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  log "Virtual environment missing at ${VENV_PYTHON}; recreating it with ${BOOTSTRAP_PYTHON_BIN}."
  "${BOOTSTRAP_PYTHON_BIN}" -m venv .venv
fi

log "Refreshing Python dependencies in the project venv."
"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -r requirements.txt

declare -a CHANGED_UNITS=()
for unit_path in deploy/systemd/*.service deploy/systemd/*.timer; do
  render_unit_if_changed "${unit_path}"
done

if ((${#CHANGED_UNITS[@]} > 0)); then
  log "Reloading systemd because unit files changed: ${CHANGED_UNITS[*]}"
  sudo systemctl daemon-reload
else
  log "Systemd unit files are unchanged."
fi

log "Restarting the API service."
sudo systemctl restart money-api.service

restart_timer_if_enabled_or_active money-news.timer
restart_timer_if_enabled_or_active money-retrain.timer

API_STATE="$(sudo systemctl is-active money-api.service || true)"
HEALTH_PAYLOAD="$(curl --silent --show-error --fail --max-time 10 http://127.0.0.1:8000/health || true)"
AUTO_STATUS_PAYLOAD="$(curl --silent --show-error --fail --max-time 10 http://127.0.0.1:8000/auto/status || true)"
NEWS_NEXT_RUN="$(timer_next_run money-news.timer)"
RETRAIN_NEXT_RUN="$(timer_next_run money-retrain.timer)"

log "Post-deploy summary:"
log "  revision_before=${PREVIOUS_HEAD}"
log "  revision_after=${CURRENT_HEAD}"
log "  api_state=${API_STATE}"
log "  health=${HEALTH_PAYLOAD:-unavailable}"
log "  auto_status=${AUTO_STATUS_PAYLOAD:-unavailable}"
log "  money-news.timer_next=${NEWS_NEXT_RUN:-unavailable}"
log "  money-retrain.timer_next=${RETRAIN_NEXT_RUN:-unavailable}"
