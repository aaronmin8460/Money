from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

try:
    from sklearn.base import BaseEstimator, ClassifierMixin
except Exception:  # pragma: no cover - train_model reports missing sklearn separately
    class ClassifierMixin:  # type: ignore[no-redef]
        pass

    class BaseEstimator:  # type: ignore[no-redef]
        pass


SUPPORTED_MODEL_TYPES = ("logistic_regression", "xgboost", "lightgbm", "ensemble")
ENSEMBLE_COMPONENT_MODEL_TYPES = ("logistic_regression", "xgboost", "lightgbm")


@dataclass(frozen=True)
class ModelEstimatorResolution:
    estimator: Any
    requested_model_type: str
    selected_model_type: str
    component_model_types: list[str] = field(default_factory=list)
    requested_component_model_types: list[str] = field(default_factory=list)
    component_resolutions: list[dict[str, Any]] = field(default_factory=list)
    fallback_reason: str | None = None

    @property
    def used_fallback(self) -> bool:
        if self.requested_model_type != self.selected_model_type:
            return True
        return any(bool(component.get("used_fallback")) for component in self.component_resolutions)

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "requested_model_type": self.requested_model_type,
            "resolved_model_type": self.selected_model_type,
            "used_fallback": self.used_fallback,
        }
        if self.fallback_reason:
            payload["fallback_reason"] = self.fallback_reason
        if self.requested_component_model_types:
            payload["requested_component_model_types"] = list(self.requested_component_model_types)
        if self.component_model_types:
            payload["component_model_types"] = list(self.component_model_types)
        if self.component_resolutions:
            payload["component_resolutions"] = list(self.component_resolutions)
        return payload


class ProbabilityAveragingEnsemble(ClassifierMixin, BaseEstimator):
    def __init__(self, estimators: list[tuple[str, Any]], component_model_types: list[str] | None = None):
        self.estimators = list(estimators)
        self.component_model_types = list(component_model_types or [name for name, _ in estimators])

    def fit(self, frame: Any, labels: list[int]) -> "ProbabilityAveragingEnsemble":
        self.classes_ = np.unique(labels)
        self.fitted_estimators_ = []
        for name, estimator in self.estimators:
            self.fitted_estimators_.append((name, estimator.fit(frame, labels)))
        return self

    def predict_proba(self, frame: Any) -> np.ndarray:
        estimators = getattr(self, "fitted_estimators_", self.estimators)
        if not estimators:
            raise ValueError("Ensemble requires at least one estimator.")
        probabilities = [np.asarray(estimator.predict_proba(frame), dtype=float) for _, estimator in estimators]
        return np.mean(probabilities, axis=0)


def normalize_model_type(model_type: str | None) -> str:
    return str(model_type or "logistic_regression").strip().lower()


def build_model_estimator(model_type: str, logistic_regression_cls: type[Any]) -> tuple[Any, str, list[str]]:
    resolution = resolve_model_estimator(model_type, logistic_regression_cls)
    return resolution.estimator, resolution.selected_model_type, resolution.component_model_types


def resolve_model_estimator(model_type: str, logistic_regression_cls: type[Any]) -> ModelEstimatorResolution:
    normalized_type = normalize_model_type(model_type)
    if normalized_type == "ensemble":
        estimators: list[tuple[str, Any]] = []
        component_types: list[str] = []
        component_resolutions: list[dict[str, Any]] = []
        component_fallbacks: list[str] = []
        for component_type in ENSEMBLE_COMPONENT_MODEL_TYPES:
            resolution = resolve_model_estimator(component_type, logistic_regression_cls)
            estimators.append((component_type, resolution.estimator))
            component_types.append(resolution.selected_model_type)
            component_resolutions.append(resolution.to_metadata())
            if resolution.used_fallback:
                reason = resolution.fallback_reason or "component estimator fell back without a reason"
                component_fallbacks.append(
                    f"{resolution.requested_model_type}->{resolution.selected_model_type} ({reason})"
                )
        fallback_reason = None
        if component_fallbacks:
            fallback_reason = "One or more ensemble components were unavailable and fell back: " + "; ".join(
                component_fallbacks
            )
        return ModelEstimatorResolution(
            estimator=ProbabilityAveragingEnsemble(estimators, component_model_types=component_types),
            requested_model_type="ensemble",
            selected_model_type="ensemble",
            component_model_types=component_types,
            requested_component_model_types=list(ENSEMBLE_COMPONENT_MODEL_TYPES),
            component_resolutions=component_resolutions,
            fallback_reason=fallback_reason,
        )
    if normalized_type == "xgboost":
        return _resolve_optional_estimator(
            normalized_type,
            logistic_regression_cls,
            _build_xgboost_estimator,
        )
    if normalized_type == "lightgbm":
        return _resolve_optional_estimator(
            normalized_type,
            logistic_regression_cls,
            _build_lightgbm_estimator,
        )
    return ModelEstimatorResolution(
        estimator=_build_logistic_estimator(logistic_regression_cls),
        requested_model_type=normalized_type,
        selected_model_type="logistic_regression",
        fallback_reason=(
            None
            if normalized_type == "logistic_regression"
            else f"unsupported model_type={normalized_type}; falling back to logistic_regression"
        ),
    )


def _resolve_optional_estimator(
    requested_model_type: str,
    logistic_regression_cls: type[Any],
    estimator_builder: Callable[[], Any],
) -> ModelEstimatorResolution:
    try:
        estimator = estimator_builder()
    except Exception as exc:
        return ModelEstimatorResolution(
            estimator=_build_logistic_estimator(logistic_regression_cls),
            requested_model_type=requested_model_type,
            selected_model_type="logistic_regression",
            fallback_reason=f"{requested_model_type} unavailable: {type(exc).__name__}: {exc}",
        )
    return ModelEstimatorResolution(
        estimator=estimator,
        requested_model_type=requested_model_type,
        selected_model_type=requested_model_type,
    )


def _build_logistic_estimator(logistic_regression_cls: type[Any]) -> Any:
    return logistic_regression_cls(max_iter=500, class_weight="balanced")


def _build_xgboost_estimator() -> Any:
    from xgboost import XGBClassifier

    return XGBClassifier(
        n_estimators=50,
        max_depth=3,
        learning_rate=0.1,
        subsample=0.9,
        colsample_bytree=0.9,
        eval_metric="logloss",
        random_state=42,
    )


def _build_lightgbm_estimator() -> Any:
    from lightgbm import LGBMClassifier

    return LGBMClassifier(
        n_estimators=50,
        max_depth=3,
        learning_rate=0.1,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=42,
        verbosity=-1,
    )
