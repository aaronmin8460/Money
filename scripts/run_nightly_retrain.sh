#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-${APP_DIR}/.venv/bin/python}"
STEP_OUTPUT=""

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  printf '%s | %s\n' "$(timestamp)" "$*"
}

run_step() {
  local step_name="$1"
  shift
  local output
  log "nightly_retrain_step_started=${step_name}"
  if ! output="$("$@" 2>&1)"; then
    if [[ -n "${output}" ]]; then
      printf '%s\n' "${output}"
    fi
    log "nightly_retrain_step_failed=${step_name}"
    return 1
  fi
  if [[ -n "${output}" ]]; then
    printf '%s\n' "${output}"
  fi
  STEP_OUTPUT="${output}"
  log "nightly_retrain_step_completed=${step_name}"
}

if [[ "${ML_RETRAIN_ENABLED:-false}" != "true" ]]; then
  log "nightly_retrain_skipped=true reason=ml_retrain_disabled"
  exit 0
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  log "nightly_retrain_failed=true reason=missing_python_bin python_bin=${PYTHON_BIN}"
  exit 1
fi

cd "${APP_DIR}"

log "nightly_retrain_started=true app_dir=${APP_DIR} python_bin=${PYTHON_BIN}"

run_step export_training_data "${PYTHON_BIN}" scripts/export_training_data.py
if grep -q "exported_rows=0" <<<"${STEP_OUTPUT}"; then
  log "nightly_retrain_skipped=true reason=no_training_rows"
  exit 0
fi

run_step train_models "${PYTHON_BIN}" scripts/train_model.py --purpose all
if grep -q "train_skipped=true reason=no_models_trained" <<<"${STEP_OUTPUT}"; then
  log "nightly_retrain_skipped=true reason=no_models_trained"
  exit 0
fi

run_step evaluate_models "${PYTHON_BIN}" scripts/evaluate_model.py --purpose all
run_step promote_entry_model "${PYTHON_BIN}" scripts/promote_model.py --purpose entry
run_step promote_exit_model "${PYTHON_BIN}" scripts/promote_model.py --purpose exit

log "nightly_retrain_completed=true"
