from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd

from app.ml.features import CATEGORICAL_FEATURES, NUMERIC_FEATURES, feature_dict


@dataclass(frozen=True)
class PreparedFeatureFrame:
    frame: pd.DataFrame
    categorical_features: list[str]
    numeric_features: list[str]
    missing_numeric_features: list[str]


def resolve_feature_columns(model_bundle: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    bundle = model_bundle or {}
    categorical = list(bundle.get("categorical_features") or CATEGORICAL_FEATURES)
    numeric = list(bundle.get("numeric_features") or NUMERIC_FEATURES)
    return categorical, numeric


def _normalize_numeric_value(value: Any) -> Any:
    if value is None:
        return np.nan
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else np.nan
    return value


def prepare_feature_frame(
    rows: Iterable[dict[str, Any] | Any],
    *,
    model_bundle: dict[str, Any] | None = None,
    fill_missing_numeric: float | None = None,
) -> PreparedFeatureFrame:
    feature_rows = [feature_dict(row) for row in rows]
    frame = pd.DataFrame(feature_rows)
    categorical_features, numeric_features = resolve_feature_columns(model_bundle)

    for column in categorical_features:
        if column not in frame:
            frame[column] = ""
        frame[column] = frame[column].where(frame[column].notna(), "").astype(str)

    missing_numeric_features: list[str] = []
    row_count = len(frame.index)
    for column in numeric_features:
        if column not in frame:
            series = pd.Series(np.nan, index=frame.index, dtype=float)
            if row_count > 0:
                missing_numeric_features.append(column)
        else:
            series = frame[column].map(_normalize_numeric_value)
            series = pd.to_numeric(series, errors="coerce")
            series = series.mask(~np.isfinite(series), np.nan)
            if bool(series.isna().any()):
                missing_numeric_features.append(column)
        if fill_missing_numeric is not None:
            series = series.fillna(fill_missing_numeric)
        frame[column] = series.astype(float)

    ordered_columns = categorical_features + numeric_features
    ordered_frame = frame.reindex(columns=ordered_columns)
    return PreparedFeatureFrame(
        frame=ordered_frame,
        categorical_features=categorical_features,
        numeric_features=numeric_features,
        missing_numeric_features=missing_numeric_features,
    )


def model_uses_internal_preprocessing(model: Any) -> bool:
    named_steps = getattr(model, "named_steps", None)
    return bool(named_steps and "preprocessor" in named_steps)
