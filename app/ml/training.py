from __future__ import annotations

import pickle
import random
from pathlib import Path
from typing import Any

from app.ml.evaluation import evaluate_predictions
from app.ml.features import CATEGORICAL_FEATURES, NUMERIC_FEATURES, feature_dict
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
        return
    with target.open("wb") as handle:
        pickle.dump(bundle, handle)


def load_model_bundle(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if joblib is not None:
        return joblib.load(source)
    with source.open("rb") as handle:
        return pickle.load(handle)


def _split_rows(rows: list[dict[str, Any]], validation_ratio: float = 0.2) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
) -> dict[str, Any]:
    labeled_rows = [feature_dict(row) for row in rows if feature_dict(row).get("label") in {0, 1}]
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

    selected_type = model_type
    estimator: Any
    if model_type == "xgboost":
        try:
            from xgboost import XGBClassifier

            estimator = XGBClassifier(
                n_estimators=50,
                max_depth=3,
                learning_rate=0.1,
                subsample=0.9,
                colsample_bytree=0.9,
                eval_metric="logloss",
                random_state=42,
            )
        except Exception:
            selected_type = "logistic_regression"
            estimator = LogisticRegression(max_iter=500, class_weight="balanced")
    else:
        estimator = LogisticRegression(max_iter=500, class_weight="balanced")

    train_rows, validation_rows = _split_rows(labeled_rows)
    train_frame = prepare_feature_frame(train_rows).frame
    validation_frame = prepare_feature_frame(validation_rows).frame
    y_train = [int(row["label"]) for row in train_rows]
    y_validation = [int(row["label"]) for row in validation_rows]

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
    metrics = evaluate_predictions(y_validation, validation_scores, threshold=threshold)
    bundle = {
        "model": pipeline,
        "model_type": selected_type,
        "feature_version": FEATURE_VERSION,
        "categorical_features": list(CATEGORICAL_FEATURES),
        "numeric_features": list(NUMERIC_FEATURES),
        "feature_columns": list(CATEGORICAL_FEATURES + NUMERIC_FEATURES),
        "threshold": threshold,
        "metrics": metrics,
    }
    return {
        "trained": True,
        "reason": None,
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "metrics": metrics,
        "bundle": bundle,
    }
