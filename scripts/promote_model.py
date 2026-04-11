from __future__ import annotations

import argparse

from app.config.settings import get_settings
from app.ml.registry import load_registry, promote_candidate, rollback_candidate


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="Promote the candidate ML model when thresholds pass.")


def main() -> None:
    _ = build_parser().parse_args()
    settings = get_settings()
    registry = load_registry(settings.ml_registry_path)
    candidate = registry.get("candidate_model")
    if not candidate:
        print("promotion_skipped=true reason=no_candidate_model")
        return

    metrics = candidate.get("metrics") or {}
    auc = metrics.get("auc")
    precision = metrics.get("precision")
    candidate_winrate = metrics.get("winrate") or 0.0
    current_winrate = ((registry.get("current_model") or {}).get("metrics") or {}).get("winrate") or 0.0
    winrate_lift = candidate_winrate - current_winrate

    auc_passes = auc is not None and float(auc) >= settings.ml_promotion_min_auc
    precision_passes = precision is not None and float(precision) >= settings.ml_promotion_min_precision
    winrate_passes = float(winrate_lift) >= settings.ml_promotion_min_winrate_lift
    if auc_passes and precision_passes and winrate_passes:
        promote_candidate(
            settings.ml_registry_path,
            current_model_path=settings.ml_current_model_path,
            candidate_model_path=settings.ml_candidate_model_path,
            notes="Candidate met promotion thresholds.",
        )
        print(
            "promotion_skipped=false "
            f"auc={auc} precision={precision} winrate_lift={winrate_lift}"
        )
        return

    rollback_candidate(
        settings.ml_registry_path,
        notes=(
            "Candidate kept as non-current because promotion thresholds were not met. "
            f"auc={auc} precision={precision} winrate_lift={winrate_lift}"
        ),
    )
    print(
        "promotion_skipped=true "
        f"auc={auc} precision={precision} winrate_lift={winrate_lift}"
    )


if __name__ == "__main__":
    main()
