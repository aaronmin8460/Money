from __future__ import annotations

from typing import Any

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
    normalized_type = normalize_model_type(model_type)
    if normalized_type == "ensemble":
        estimators: list[tuple[str, Any]] = []
        component_types: list[str] = []
        for component_type in ENSEMBLE_COMPONENT_MODEL_TYPES:
            estimator, selected_type, _ = build_model_estimator(component_type, logistic_regression_cls)
            estimators.append((component_type, estimator))
            component_types.append(selected_type)
        return ProbabilityAveragingEnsemble(estimators, component_model_types=component_types), "ensemble", component_types
    if normalized_type == "xgboost":
        estimator = _build_xgboost_estimator()
        if estimator is not None:
            return estimator, "xgboost", []
    if normalized_type == "lightgbm":
        estimator = _build_lightgbm_estimator()
        if estimator is not None:
            return estimator, "lightgbm", []
    return _build_logistic_estimator(logistic_regression_cls), "logistic_regression", []


def _build_logistic_estimator(logistic_regression_cls: type[Any]) -> Any:
    return logistic_regression_cls(max_iter=500, class_weight="balanced")


def _build_xgboost_estimator() -> Any | None:
    try:
        from xgboost import XGBClassifier
    except Exception:
        return None
    return XGBClassifier(
        n_estimators=50,
        max_depth=3,
        learning_rate=0.1,
        subsample=0.9,
        colsample_bytree=0.9,
        eval_metric="logloss",
        random_state=42,
    )


def _build_lightgbm_estimator() -> Any | None:
    try:
        from lightgbm import LGBMClassifier
    except Exception:
        return None
    return LGBMClassifier(
        n_estimators=50,
        max_depth=3,
        learning_rate=0.1,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=42,
        verbosity=-1,
    )
