from __future__ import annotations

import logging
import pickle
import random
from pathlib import Path
from typing import Any

from app.ml.evaluation import calibrate_threshold, evaluate_rows, walk_forward_split
from app.ml.diagnostics import build_shap_feature_importance, save_feature_importance_artifact
from app.ml.features import CATEGORICAL_FEATURES, NUMERIC_FEATURES, feature_dict
from app.ml.model_selection import normalize_model_type, resolve_model_estimator
from app.ml.preprocessing import prepare_feature_frame
from app.ml.schema import FEATURE_VERSION
from app.monitoring.logger import get_logger

try:
    import joblib
except Exception:  # pragma: no cover - fallback when optional dependency missing
    joblib = None


logger = get_logger("ml.training")

_ESTIMATOR_CLASS_MODEL_TYPES = {
    "LogisticRegression": "logistic_regression",
    "XGBClassifier": "xgboost",
    "LGBMClassifier": "lightgbm",
    "ProbabilityAveragingEnsemble": "ensemble",
}


def _emit_training_warning(message: str, *, extra: dict[str, Any]) -> None:
    logger.warning(message, extra=extra)
    root_logger = logging.getLogger()
    if any(handler.__class__.__module__.startswith("_pytest.") for handler in root_logger.handlers):
        root_logger.warning(message)


def _base_estimator(model: Any) -> Any:
    named_steps = getattr(model, "named_steps", None)
    if named_steps and "model" in named_steps:
        return named_steps["model"]
    return model


def _infer_model_type_from_model(model: Any) -> str | None:
    estimator = _base_estimator(model)
    return _ESTIMATOR_CLASS_MODEL_TYPES.get(estimator.__class__.__name__)


def normalize_model_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(bundle)
    model = normalized.get("model")
    inferred_model_type = normalize_model_type(normalized.get("model_type") or _infer_model_type_from_model(model))
    normalized["model_type"] = inferred_model_type
    requested_model_type = normalize_model_type(normalized.get("requested_model_type") or inferred_model_type)
    normalized["requested_model_type"] = requested_model_type
    base_estimator_class = normalized.get("base_estimator_class") or _base_estimator(model).__class__.__name__
    normalized["base_estimator_class"] = str(base_estimator_class)

    model_selection = normalized.get("model_selection")
    if isinstance(model_selection, dict):
        selection_metadata = dict(model_selection)
    else:
        selection_metadata = {}
    selection_metadata.setdefault("requested_model_type", requested_model_type)
    selection_metadata.setdefault("resolved_model_type", inferred_model_type)
    selection_metadata["used_fallback"] = bool(
        selection_metadata.get("used_fallback")
        or selection_metadata["requested_model_type"] != selection_metadata["resolved_model_type"]
    )
    if normalized.get("component_model_types"):
        selection_metadata.setdefault("component_model_types", list(normalized["component_model_types"]))
    normalized["model_selection"] = selection_metadata
    return normalized


def save_model_bundle(bundle: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized_bundle = normalize_model_bundle(bundle)
    if joblib is not None:
        joblib.dump(normalized_bundle, target)
    else:
        with target.open("wb") as handle:
            pickle.dump(normalized_bundle, handle)
    try:
        save_feature_importance_artifact(normalized_bundle, target)
    except Exception:
        # Diagnostics artifacts should not block model persistence.
        pass


def load_model_bundle(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if joblib is not None:
        bundle = joblib.load(source)
    else:
        with source.open("rb") as handle:
            bundle = pickle.load(handle)
    if not isinstance(bundle, dict):
        bundle = {"model": bundle}
    return normalize_model_bundle(bundle)


def _split_rows(
    rows: list[dict[str, Any]],
    validation_ratio: float = 0.2,
    *,
    walk_forward_enabled: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if walk_forward_enabled:
        return walk_forward_split(rows, validation_ratio=validation_ratio)
    shuffled = list(rows)
    random.Random(42).shuffle(shuffled)
    validation_size = max(1, int(len(shuffled) * validation_ratio))
    if validation_size >= len(shuffled):
        validation_size = max(1, len(shuffled) // 2)
    validation = shuffled[:validation_size]
    training = shuffled[validation_size:] or shuffled[:]
    return training, validation


def train_model(
    rows: list[dict[str, Any]],
    *,
    model_type: str = "logistic_regression",
    min_train_rows: int = 50,
    threshold: float = 0.5,
    purpose: str = "entry",
    walk_forward_enabled: bool = False,
    min_precision: float = 0.0,
) -> dict[str, Any]:
    labeled_rows = [
        feature_dict(row)
        for row in rows
        if feature_dict(row).get("label") in {0, 1}
        and str(feature_dict(row).get("model_purpose") or "entry") == purpose
    ]
    if len(labeled_rows) < min_train_rows:
        return {
            "trained": False,
            "reason": f"Not enough labeled rows for training ({len(labeled_rows)} < {min_train_rows}).",
            "train_rows": len(labeled_rows),
            "validation_rows": 0,
            "metrics": {},
            "bundle": None,
        }

    labels = [int(row["label"]) for row in labeled_rows]
    if len(set(labels)) < 2:
        return {
            "trained": False,
            "reason": "Training requires both positive and negative labels.",
            "train_rows": len(labeled_rows),
            "validation_rows": 0,
            "metrics": {},
            "bundle": None,
        }

    try:
        from sklearn.compose import ColumnTransformer
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder, StandardScaler
    except Exception as exc:
        return {
            "trained": False,
            "reason": f"scikit-learn is unavailable: {exc}",
            "train_rows": len(labeled_rows),
            "validation_rows": 0,
            "metrics": {},
            "bundle": None,
        }

    resolution = resolve_model_estimator(model_type, LogisticRegression)
    estimator = resolution.estimator
    selected_type = resolution.selected_model_type
    component_model_types = resolution.component_model_types
    if resolution.used_fallback:
        _emit_training_warning(
            "Requested ML estimator could not be used; falling back to the resolved estimator",
            extra=resolution.to_metadata(),
        )

    train_rows, validation_rows = _split_rows(labeled_rows, walk_forward_enabled=walk_forward_enabled)
    train_frame = prepare_feature_frame(train_rows).frame
    validation_frame = prepare_feature_frame(validation_rows).frame
    y_train = [int(row["label"]) for row in train_rows]

    preprocessor = ColumnTransformer(
        transformers=[
            (
                "categorical",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="constant", fill_value="")),
                        ("one_hot", OneHotEncoder(handle_unknown="ignore")),
                    ]
                ),
                CATEGORICAL_FEATURES,
            ),
            (
                "numeric",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="constant", fill_value=0.0)),
                        ("scale", StandardScaler()),
                    ]
                ),
                NUMERIC_FEATURES,
            ),
        ]
    )
    pipeline = Pipeline(
        steps=[
            ("preprocessor", preprocessor),
            ("model", estimator),
        ]
    )
    pipeline.fit(train_frame[CATEGORICAL_FEATURES + NUMERIC_FEATURES], y_train)
    validation_scores = [
        float(row[1])
        for row in pipeline.predict_proba(validation_frame[CATEGORICAL_FEATURES + NUMERIC_FEATURES])
    ]
    calibration = calibrate_threshold(
        [int(row["label"]) for row in validation_rows],
        validation_scores,
        outcome_returns=[
            row.get("realized_return", row.get("forward_return", row.get("risk_adjusted_return")))
            for row in validation_rows
        ],
        min_precision=min_precision,
    )
    threshold = float(calibration["threshold"])
    metrics = evaluate_rows(validation_rows, validation_scores, threshold=threshold)
    bundle = {
        "model": pipeline,
        "model_type": selected_type,
        "requested_model_type": resolution.requested_model_type,
        "purpose": purpose,
        "feature_version": FEATURE_VERSION,
        "categorical_features": list(CATEGORICAL_FEATURES),
        "numeric_features": list(NUMERIC_FEATURES),
        "feature_columns": list(CATEGORICAL_FEATURES + NUMERIC_FEATURES),
        "threshold": threshold,
        "metrics": metrics,
        "base_estimator_class": pipeline.named_steps["model"].__class__.__name__,
        "model_selection": resolution.to_metadata(),
    }
    if component_model_types:
        bundle["component_model_types"] = component_model_types
    bundle["feature_importance"] = build_shap_feature_importance(
        model=pipeline,
        rows=train_rows,
        model_type=selected_type,
        purpose=purpose,
        feature_version=FEATURE_VERSION,
        categorical_features=list(CATEGORICAL_FEATURES),
        numeric_features=list(NUMERIC_FEATURES),
    )
    bundle = normalize_model_bundle(bundle)
    return {
        "trained": True,
        "reason": None,
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "metrics": metrics,
        "bundle": bundle,
    }
