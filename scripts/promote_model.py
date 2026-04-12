from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from app.config.settings import get_settings
from app.ml.evaluation import promotion_thresholds_pass
from app.ml.registry import load_registry, promote_candidate, rollback_candidate


def candidate_meets_promotion_thresholds(
    settings,
    metrics: dict[str, object],
    *,
    current_metrics: dict[str, object] | None = None,
) -> tuple[bool, dict[str, float | None]]:
    return promotion_thresholds_pass(settings, metrics, current_metrics=current_metrics)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Promote the candidate ML model when thresholds pass.")
    parser.add_argument("--purpose", choices=["entry", "exit"], default="entry")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    registry = load_registry(settings.ml_registry_path)
    purpose = args.purpose
    purpose_registry = registry.get("models", {}).get(purpose, {})
    candidate = purpose_registry.get("candidate_model")
    if not candidate:
        print(f"promotion_skipped=true purpose={purpose} reason=no_candidate_model")
        return

    metrics = candidate.get("metrics") or {}
    current_metrics = (purpose_registry.get("current_model") or {}).get("metrics") or None
    passed, metric_snapshot = candidate_meets_promotion_thresholds(
        settings,
        metrics,
        current_metrics=current_metrics,
    )
    if passed:
        current_model_path = settings.ml_entry_current_model_path if purpose == "entry" else settings.ml_exit_current_model_path
        candidate_model_path = (
            settings.ml_entry_candidate_model_path if purpose == "entry" else settings.ml_exit_candidate_model_path
        )
        promote_candidate(
            settings.ml_registry_path,
            current_model_path=current_model_path,
            candidate_model_path=candidate_model_path,
            notes=(
                f"{purpose} candidate met promotion thresholds. "
                "Absolute ML/trading floors passed"
                + (
                    f" and win-rate lift vs current ({metric_snapshot['winrate_lift']:.4f}) cleared the requirement."
                    if metric_snapshot.get("winrate_lift") is not None
                    else "; no current same-purpose win-rate baseline was available."
                )
            ),
            model_purpose=purpose,
        )
        print(
            "promotion_skipped=false "
            f"purpose={purpose} metrics={metric_snapshot}"
        )
        return

    rollback_candidate(
        settings.ml_registry_path,
        notes=(
            "Candidate kept as non-current because promotion thresholds were not met. "
            f"purpose={purpose} metrics={metric_snapshot}"
        ),
        model_purpose=purpose,
    )
    print(
        "promotion_skipped=true "
        f"purpose={purpose} metrics={metric_snapshot}"
    )


if __name__ == "__main__":
    main()
