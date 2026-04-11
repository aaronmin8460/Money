from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.config.settings import get_settings
from app.ml.evaluation import evaluate_predictions, predict_scores
from app.ml.registry import load_registry, save_registry
from app.ml.training import load_model_bundle


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


def _evaluate_bundle(path: Path, rows: list[dict[str, Any]], threshold: float) -> dict[str, Any] | None:
    if not path.exists():
        return None
    bundle = load_model_bundle(path)
    labeled_rows = [row for row in rows if row.get("label") in {0, 1}]
    if not labeled_rows:
        return None
    scores = predict_scores(bundle, labeled_rows)
    labels = [int(row["label"]) for row in labeled_rows]
    return evaluate_predictions(labels, scores, threshold=threshold)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate current and candidate ML models.")
    parser.add_argument("--dataset", default="models/training_data.jsonl", help="Training/evaluation dataset JSONL path.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    rows = _load_jsonl(Path(args.dataset))
    if not rows:
        print("evaluation_skipped=true reason=no_dataset_rows")
        return

    registry = load_registry(settings.ml_registry_path)
    candidate_metrics = _evaluate_bundle(Path(settings.ml_candidate_model_path), rows, settings.ml_min_score_threshold)
    current_metrics = _evaluate_bundle(Path(settings.ml_current_model_path), rows, settings.ml_min_score_threshold)
    registry["evaluation"] = {
        "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_path": str(Path(args.dataset).resolve()),
        "candidate_metrics": candidate_metrics,
        "current_metrics": current_metrics,
        "dataset_rows": len(rows),
    }
    if registry.get("candidate_model") and candidate_metrics is not None:
        registry["candidate_model"]["metrics"] = candidate_metrics
    if registry.get("current_model") and current_metrics is not None:
        registry["current_model"]["metrics"] = current_metrics
    save_registry(settings.ml_registry_path, registry)
    print(
        "evaluation_skipped=false "
        f"candidate_metrics={candidate_metrics} "
        f"current_metrics={current_metrics}"
    )


if __name__ == "__main__":
    main()
