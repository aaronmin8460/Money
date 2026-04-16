from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.ml.preprocessing import prepare_feature_frame

SHAP_ARTIFACT_NAME = "shap_values.json"
MAX_SHAP_BACKGROUND_ROWS = 50
MAX_SHAP_SAMPLE_ROWS = 100
MAX_DIAGNOSTIC_FEATURES = 20


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unavailable_feature_importance(reason: str, *, purpose: str = "entry", model_type: str | None = None) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "purpose": purpose,
        "model_type": model_type,
        "generated_at": _utc_now(),
        "source": "shap_mean_abs",
        "top_features": [],
    }


def _to_dense_array(matrix: Any) -> np.ndarray:
    if hasattr(matrix, "toarray"):
        matrix = matrix.toarray()
    return np.asarray(matrix, dtype=float)


def _coerce_shap_values(values: Any) -> np.ndarray | None:
    if isinstance(values, list):
        if not values:
            return None
        class_arrays = [np.asarray(item, dtype=float) for item in values]
        if all(array.ndim == 2 for array in class_arrays):
            array = class_arrays[1] if len(class_arrays) > 1 else class_arrays[0]
        else:
            array = np.asarray(values, dtype=float)
    else:
        array = np.asarray(values, dtype=float)
    if array.ndim == 3:
        array = array[..., 1] if array.shape[-1] > 1 else array[..., 0]
    if array.ndim != 2 or array.shape[1] == 0:
        return None
    return array


def _feature_name_from_encoded(
    encoded_name: str,
    *,
    categorical_features: list[str],
    numeric_features: list[str],
) -> str:
    name = encoded_name.split("__", 1)[1] if "__" in encoded_name else encoded_name
    if name in numeric_features:
        return name
    for feature in sorted(categorical_features, key=len, reverse=True):
        if name == feature or name.startswith(f"{feature}_"):
            return feature
    return name


def _encoded_feature_names(preprocessor: Any, fallback_features: list[str]) -> list[str]:
    try:
        return [str(name) for name in preprocessor.get_feature_names_out()]
    except Exception:
        return list(fallback_features)


def build_shap_feature_importance(
    *,
    model: Any,
    rows: list[dict[str, Any]],
    model_type: str,
    purpose: str,
    feature_version: str,
    categorical_features: list[str],
    numeric_features: list[str],
    max_features: int = MAX_DIAGNOSTIC_FEATURES,
) -> dict[str, Any]:
    if not rows:
        return _unavailable_feature_importance(
            "No training rows were available for SHAP feature importance.",
            purpose=purpose,
            model_type=model_type,
        )

    try:
        import shap
    except Exception as exc:
        return _unavailable_feature_importance(
            f"SHAP is unavailable: {type(exc).__name__}",
            purpose=purpose,
            model_type=model_type,
        )

    named_steps = getattr(model, "named_steps", None)
    preprocessor = named_steps.get("preprocessor") if named_steps else None
    estimator = named_steps.get("model") if named_steps else None
    if preprocessor is None or estimator is None:
        return _unavailable_feature_importance(
            "Model pipeline does not expose a supported preprocessor/model pair.",
            purpose=purpose,
            model_type=model_type,
        )
    if model_type == "ensemble":
        return _unavailable_feature_importance(
            "SHAP diagnostics are not generated for ensemble models yet.",
            purpose=purpose,
            model_type=model_type,
        )

    model_bundle = {
        "categorical_features": categorical_features,
        "numeric_features": numeric_features,
    }
    prepared = prepare_feature_frame(rows, model_bundle=model_bundle)
    feature_frame = prepared.frame[categorical_features + numeric_features]
    if feature_frame.empty:
        return _unavailable_feature_importance(
            "No feature rows were available for SHAP feature importance.",
            purpose=purpose,
            model_type=model_type,
        )

    background_frame = feature_frame.head(MAX_SHAP_BACKGROUND_ROWS)
    sample_frame = feature_frame.head(MAX_SHAP_SAMPLE_ROWS)
    try:
        background = _to_dense_array(preprocessor.transform(background_frame))
        sample = _to_dense_array(preprocessor.transform(sample_frame))
        if model_type == "logistic_regression":
            explainer = shap.LinearExplainer(estimator, background)
            raw_values = explainer.shap_values(sample)
        elif model_type in {"xgboost", "lightgbm"}:
            explainer = shap.TreeExplainer(estimator)
            raw_values = explainer.shap_values(sample)
        else:
            explainer = shap.Explainer(estimator, background)
            explanation = explainer(sample)
            raw_values = getattr(explanation, "values", explanation)
    except Exception as exc:
        return _unavailable_feature_importance(
            f"SHAP feature importance generation failed: {type(exc).__name__}",
            purpose=purpose,
            model_type=model_type,
        )

    shap_values = _coerce_shap_values(raw_values)
    if shap_values is None:
        return _unavailable_feature_importance(
            "SHAP returned an unsupported value shape.",
            purpose=purpose,
            model_type=model_type,
        )

    encoded_names = _encoded_feature_names(preprocessor, categorical_features + numeric_features)
    if len(encoded_names) != shap_values.shape[1]:
        encoded_names = [f"feature_{index}" for index in range(shap_values.shape[1])]

    mean_abs_values = np.mean(np.abs(shap_values), axis=0)
    grouped: dict[str, float] = {}
    for encoded_name, importance in zip(encoded_names, mean_abs_values):
        if not math.isfinite(float(importance)):
            continue
        feature_name = _feature_name_from_encoded(
            encoded_name,
            categorical_features=categorical_features,
            numeric_features=numeric_features,
        )
        grouped[feature_name] = grouped.get(feature_name, 0.0) + float(importance)

    top_features = [
        {"feature": feature, "importance": round(importance, 8)}
        for feature, importance in sorted(grouped.items(), key=lambda item: item[1], reverse=True)[:max_features]
        if importance > 0
    ]
    if not top_features:
        return _unavailable_feature_importance(
            "SHAP generated no non-zero feature importances.",
            purpose=purpose,
            model_type=model_type,
        )

    return {
        "status": "available",
        "purpose": purpose,
        "model_type": model_type,
        "feature_version": feature_version,
        "generated_at": _utc_now(),
        "source": "shap_mean_abs",
        "train_rows": len(rows),
        "sample_rows": len(sample_frame.index),
        "top_features": top_features,
    }


def feature_importance_artifact_path_for_model(model_path: str | Path) -> Path:
    return Path(model_path).with_name(SHAP_ARTIFACT_NAME)


def _compact_feature_rows(features: Any, *, limit: int = MAX_DIAGNOSTIC_FEATURES) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    if not isinstance(features, list):
        return compact
    for row in features[:limit]:
        if not isinstance(row, dict):
            continue
        feature = str(row.get("feature") or "").strip()
        if not feature:
            continue
        try:
            importance = float(row.get("importance"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(importance):
            continue
        compact.append({"feature": feature, "importance": round(importance, 8)})
    return compact


def _compact_model_summary(payload: dict[str, Any], *, max_features: int = MAX_DIAGNOSTIC_FEATURES) -> dict[str, Any]:
    status = str(payload.get("status") or "unavailable")
    compact = {
        "status": status,
        "purpose": payload.get("purpose"),
        "model_type": payload.get("model_type"),
        "feature_version": payload.get("feature_version"),
        "generated_at": payload.get("generated_at"),
        "source": payload.get("source") or "shap_mean_abs",
        "train_rows": payload.get("train_rows"),
        "sample_rows": payload.get("sample_rows"),
        "top_features": _compact_feature_rows(payload.get("top_features"), limit=max_features),
    }
    if payload.get("reason"):
        compact["reason"] = str(payload["reason"])
    return {key: value for key, value in compact.items() if value is not None}


def save_feature_importance_artifact(bundle: dict[str, Any], model_path: str | Path) -> Path | None:
    summary = bundle.get("feature_importance")
    if not isinstance(summary, dict):
        return None

    purpose = str(summary.get("purpose") or bundle.get("purpose") or "entry").strip().lower() or "entry"
    target = feature_importance_artifact_path_for_model(model_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except json.JSONDecodeError:
            payload = {}

    models = payload.get("models") if isinstance(payload.get("models"), dict) else {}
    models[purpose] = _compact_model_summary(summary)
    any_available = any(
        isinstance(model_summary, dict) and model_summary.get("status") == "available"
        for model_summary in models.values()
    )
    artifact_payload = {
        "status": "available" if any_available else "unavailable",
        "artifact_version": 1,
        "generated_at": _utc_now(),
        "source": "shap_mean_abs",
        "models": models,
    }
    target.write_text(json.dumps(artifact_payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def resolve_feature_importance_artifact_paths(settings: Any) -> list[Path]:
    raw_paths = [
        Path(str(settings.model_dir)) / SHAP_ARTIFACT_NAME,
        feature_importance_artifact_path_for_model(settings.ml_entry_current_model_path),
        feature_importance_artifact_path_for_model(settings.ml_entry_candidate_model_path),
        feature_importance_artifact_path_for_model(settings.ml_exit_current_model_path),
        feature_importance_artifact_path_for_model(settings.ml_exit_candidate_model_path),
        Path(str(settings.ml_registry_path)).with_name(SHAP_ARTIFACT_NAME),
    ]
    paths: list[Path] = []
    seen: set[str] = set()
    for path in raw_paths:
        resolved_key = str(path)
        if resolved_key in seen:
            continue
        seen.add(resolved_key)
        paths.append(path)
    return paths


def load_feature_importance_diagnostics(settings: Any) -> dict[str, Any]:
    paths = resolve_feature_importance_artifact_paths(settings)
    artifact_path = next((path for path in paths if path.exists()), None)
    if artifact_path is None:
        return {
            "status": "unavailable",
            "reason": "SHAP feature importance artifact not found.",
            "models": {},
            "top_features": [],
        }

    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "reason": f"SHAP feature importance artifact could not be read: {type(exc).__name__}",
            "artifact_path": str(artifact_path),
            "models": {},
            "top_features": [],
        }
    if not isinstance(payload, dict):
        return {
            "status": "unavailable",
            "reason": "SHAP feature importance artifact has an invalid format.",
            "artifact_path": str(artifact_path),
            "models": {},
            "top_features": [],
        }

    raw_models = payload.get("models") if isinstance(payload.get("models"), dict) else {}
    if raw_models:
        models = {
            str(purpose): _compact_model_summary(model_payload)
            for purpose, model_payload in raw_models.items()
            if isinstance(model_payload, dict)
        }
    else:
        model_summary = _compact_model_summary(payload)
        purpose = str(model_summary.get("purpose") or "entry")
        models = {purpose: model_summary}

    top_features = models.get("entry", next(iter(models.values()), {})).get("top_features", []) if models else []
    any_available = any(model.get("status") == "available" and model.get("top_features") for model in models.values())
    return {
        "status": "available" if any_available else "unavailable",
        "reason": None if any_available else "SHAP feature importance data is unavailable.",
        "artifact_path": str(artifact_path),
        "generated_at": payload.get("generated_at"),
        "source": payload.get("source") or "shap_mean_abs",
        "models": models,
        "top_features": top_features,
    }
