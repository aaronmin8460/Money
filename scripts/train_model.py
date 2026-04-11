from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.config.settings import get_settings
from app.ml.registry import update_candidate
from app.ml.training import save_model_bundle, train_model


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train a candidate ML model from exported JSONL data.")
    parser.add_argument("--dataset", default="models/training_data.jsonl", help="Training dataset JSONL path.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    dataset_path = Path(args.dataset)
    rows = _load_jsonl(dataset_path)
    result = train_model(
        rows,
        model_type=settings.ml_model_type,
        min_train_rows=settings.ml_min_train_rows,
        threshold=settings.ml_min_score_threshold,
    )
    if not result["trained"]:
        print(f"train_skipped=true reason={result['reason']}")
        return

    save_model_bundle(result["bundle"], settings.ml_candidate_model_path)
    update_candidate(
        settings.ml_registry_path,
        model_path=settings.ml_candidate_model_path,
        model_type=result["bundle"]["model_type"],
        feature_version=result["bundle"]["feature_version"],
        train_rows=result["train_rows"],
        validation_rows=result["validation_rows"],
        metrics=result["metrics"],
        notes="Candidate model trained from structured outcome logs.",
    )
    print(
        "train_skipped=false "
        f"candidate_model={settings.ml_candidate_model_path} "
        f"train_rows={result['train_rows']} "
        f"validation_rows={result['validation_rows']} "
        f"metrics={result['metrics']}"
    )


if __name__ == "__main__":
    main()
