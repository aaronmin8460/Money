#!/usr/bin/env bash
set -euo pipefail

if [[ "${ML_RETRAIN_ENABLED:-false}" != "true" ]]; then
  echo "ML_RETRAIN_ENABLED is not true. Skipping nightly retrain."
  exit 0
fi

python scripts/export_training_data.py
python scripts/train_model.py
python scripts/evaluate_model.py
python scripts/promote_model.py
