from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.config.settings import get_settings
from app.ml.evaluation import evaluate_rows, predict_scores
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


def _evaluate_bundle(path: Path, rows: list[dict[str, Any]], threshold: float, purpose: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    bundle = load_model_bundle(path)
    labeled_rows = [
        row
        for row in rows
        if row.get("label") in {0, 1}
        and str(row.get("model_purpose") or "entry") == purpose
    ]
    if not labeled_rows:
        return None
    scores = predict_scores(bundle, labeled_rows)
    resolved_threshold = float(bundle.get("threshold") or threshold)
    return evaluate_rows(labeled_rows, scores, threshold=resolved_threshold)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate current and candidate ML models.")
    parser.add_argument("--dataset", default="models/training_data.jsonl", help="Training/evaluation dataset JSONL path.")
    parser.add_argument(
        "--purpose",
        choices=["entry", "exit", "all"],
        default="all",
        help="Which model purpose to evaluate.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    rows = _load_jsonl(Path(args.dataset))
    if not rows:
        print("evaluation_skipped=true reason=no_dataset_rows")
        return

    registry = load_registry(settings.ml_registry_path)
    purposes = ["entry", "exit"] if args.purpose == "all" else [args.purpose]
    evaluation_payload: dict[str, Any] = {
        "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_path": str(Path(args.dataset).resolve()),
        "dataset_rows": len(rows),
    }
    for purpose in purposes:
        current_path = (
            Path(settings.ml_entry_current_model_path) if purpose == "entry" else Path(settings.ml_exit_current_model_path)
        )
        candidate_path = (
            Path(settings.ml_entry_candidate_model_path)
            if purpose == "entry"
            else Path(settings.ml_exit_candidate_model_path)
        )
        threshold = settings.ml_min_score_threshold if purpose == "entry" else settings.ml_exit_min_score
        candidate_metrics = _evaluate_bundle(candidate_path, rows, threshold, purpose)
        current_metrics = _evaluate_bundle(current_path, rows, threshold, purpose)
        evaluation_payload[purpose] = {
            "candidate_metrics": candidate_metrics,
            "current_metrics": current_metrics,
        }
        if registry.get("models", {}).get(purpose, {}).get("candidate_model") and candidate_metrics is not None:
            registry["models"][purpose]["candidate_model"]["metrics"] = candidate_metrics
        if registry.get("models", {}).get(purpose, {}).get("current_model") and current_metrics is not None:
            registry["models"][purpose]["current_model"]["metrics"] = current_metrics
        if purpose == "entry":
            if registry.get("candidate_model") and candidate_metrics is not None:
                registry["candidate_model"]["metrics"] = candidate_metrics
            if registry.get("current_model") and current_metrics is not None:
                registry["current_model"]["metrics"] = current_metrics
    registry["evaluation"] = evaluation_payload
    save_registry(settings.ml_registry_path, registry)
    print(
        "evaluation_skipped=false "
        f"purposes={purposes} "
        f"evaluation={evaluation_payload}"
    )


if __name__ == "__main__":
    main()
