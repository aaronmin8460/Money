from __future__ import annotations

import pickle
import random
from pathlib import Path
from typing import Any

from app.ml.evaluation import calibrate_threshold, evaluate_rows, walk_forward_split
from app.ml.diagnostics import build_shap_feature_importance, save_feature_importance_artifact
from app.ml.features import CATEGORICAL_FEATURES, NUMERIC_FEATURES, feature_dict
from app.ml.model_selection import build_model_estimator
from app.ml.preprocessing import prepare_feature_frame
from app.ml.schema import FEATURE_VERSION

try:
    import joblib
except Exception:  # pragma: no cover - fallback when optional dependency missing
    joblib = None


def save_model_bundle(bundle: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if joblib is not None:
        joblib.dump(bundle, target)
    else:
        with target.open("wb") as handle:
            pickle.dump(bundle, handle)
    try:
        save_feature_importance_artifact(bundle, target)
    except Exception:
        # Diagnostics artifacts should not block model persistence.
        pass


def load_model_bundle(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if joblib is not None:
        return joblib.load(source)
    with source.open("rb") as handle:
        return pickle.load(handle)


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

    estimator, selected_type, component_model_types = build_model_estimator(model_type, LogisticRegression)

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
        "purpose": purpose,
        "feature_version": FEATURE_VERSION,
        "categorical_features": list(CATEGORICAL_FEATURES),
        "numeric_features": list(NUMERIC_FEATURES),
        "feature_columns": list(CATEGORICAL_FEATURES + NUMERIC_FEATURES),
        "threshold": threshold,
        "metrics": metrics,
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
    return {
        "trained": True,
        "reason": None,
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "metrics": metrics,
        "bundle": bundle,
    }
