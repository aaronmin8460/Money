from __future__ import annotations

from typing import Any, Iterable

from app.ml.features import feature_dict
from app.ml.preprocessing import model_uses_internal_preprocessing, prepare_feature_frame


def _binary_auc(y_true: list[int], y_score: list[float]) -> float | None:
    positives = [score for label, score in zip(y_true, y_score) if label == 1]
    negatives = [score for label, score in zip(y_true, y_score) if label == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    total = 0
    for pos in positives:
        for neg in negatives:
            total += 1
            if pos > neg:
                wins += 1.0
            elif pos == neg:
                wins += 0.5
    if total == 0:
        return None
    return wins / total


def evaluate_predictions(y_true: list[int], y_score: list[float], *, threshold: float = 0.5) -> dict[str, Any]:
    predicted = [1 if score >= threshold else 0 for score in y_score]
    tp = sum(1 for truth, pred in zip(y_true, predicted) if truth == 1 and pred == 1)
    fp = sum(1 for truth, pred in zip(y_true, predicted) if truth == 0 and pred == 1)
    fn = sum(1 for truth, pred in zip(y_true, predicted) if truth == 1 and pred == 0)
    tn = sum(1 for truth, pred in zip(y_true, predicted) if truth == 0 and pred == 0)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    accuracy = (tp + tn) / len(y_true) if y_true else 0.0
    auc = _binary_auc(y_true, y_score)
    return {
        "rows": len(y_true),
        "threshold": threshold,
        "auc": auc,
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
        "winrate": precision,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def predict_scores(model_bundle: dict[str, Any], rows: Iterable[dict[str, Any] | Any]) -> list[float]:
    model = model_bundle["model"]
    feature_rows = [feature_dict(row) for row in rows]
    prepared = prepare_feature_frame(feature_rows, model_bundle=model_bundle)
    scoring_frame = prepared.frame
    if hasattr(model, "predict_proba") and not model_uses_internal_preprocessing(model):
        scoring_frame = prepare_feature_frame(
            feature_rows,
            model_bundle=model_bundle,
            fill_missing_numeric=0.0,
        ).frame
    if hasattr(model, "predict_scores"):
        return [float(value) for value in model.predict_scores(feature_rows)]
    if hasattr(model, "predict_proba"):
        probabilities = model.predict_proba(scoring_frame)
        return [float(row[1]) for row in probabilities]
    if callable(model):
        return [float(model(row)) for row in feature_rows]
    raise TypeError("Model bundle does not expose a supported scoring interface.")
