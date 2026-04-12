from __future__ import annotations

import math
from typing import Any, Iterable

from app.ml.features import feature_dict
from app.ml.preprocessing import model_uses_internal_preprocessing, prepare_feature_frame
from app.utils.datetime_parser import parse_iso_datetime


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


def walk_forward_split(
    rows: list[dict[str, Any]],
    *,
    validation_ratio: float = 0.2,
    time_key: str = "generated_at",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []

    sortable_rows: list[tuple[Any, dict[str, Any]]] = []
    unsorted_rows: list[dict[str, Any]] = []
    for row in rows:
        raw_time = row.get(time_key)
        if raw_time in {None, ""}:
            unsorted_rows.append(row)
            continue
        try:
            parsed = parse_iso_datetime(raw_time)
        except ValueError:
            unsorted_rows.append(row)
            continue
        sortable_rows.append((parsed, row))

    ordered_rows = [row for _, row in sorted(sortable_rows, key=lambda item: item[0])] + unsorted_rows
    validation_size = max(1, int(len(ordered_rows) * validation_ratio))
    if validation_size >= len(ordered_rows):
        validation_size = max(1, len(ordered_rows) // 2)
    validation = ordered_rows[-validation_size:]
    training = ordered_rows[:-validation_size] or ordered_rows[:]
    return training, validation


def _coerce_trade_return(value: Any, label: int | None = None) -> float | None:
    if value in {None, ""}:
        if label is None:
            return None
        return 0.01 if label == 1 else -0.01
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_outcome_returns(rows: list[dict[str, Any]], y_true: list[int]) -> list[float | None]:
    outcome_returns: list[float | None] = []
    for row, label in zip(rows, y_true):
        outcome_returns.append(
            _coerce_trade_return(
                row.get("realized_return", row.get("forward_return", row.get("risk_adjusted_return"))),
                label=label,
            )
        )
    return outcome_returns


def compute_trade_metrics(trade_returns: list[float]) -> dict[str, Any]:
    if not trade_returns:
        return {
            "trade_count": 0,
            "turnover": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "average_trade_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe_like": 0.0,
        }

    gross_profit = sum(value for value in trade_returns if value > 0)
    gross_loss = abs(sum(value for value in trade_returns if value < 0))
    win_rate = sum(1 for value in trade_returns if value > 0) / len(trade_returns)
    expectancy = sum(trade_returns) / len(trade_returns)
    average_trade_return = expectancy

    equity_curve: list[float] = []
    equity = 1.0
    peak = equity
    max_drawdown = 0.0
    for trade_return in trade_returns:
        equity *= (1.0 + trade_return)
        equity_curve.append(equity)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    variance = 0.0
    if len(trade_returns) > 1:
        mean = expectancy
        variance = sum((value - mean) ** 2 for value in trade_returns) / (len(trade_returns) - 1)
    std_dev = math.sqrt(variance) if variance > 0 else 0.0
    sharpe_like = 0.0
    if std_dev > 0:
        sharpe_like = (expectancy / std_dev) * math.sqrt(len(trade_returns))
    elif expectancy > 0:
        sharpe_like = expectancy * math.sqrt(len(trade_returns))

    if gross_loss == 0:
        profit_factor = float("inf") if gross_profit > 0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss

    return {
        "trade_count": len(trade_returns),
        "turnover": len(trade_returns),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "average_trade_return": average_trade_return,
        "max_drawdown": max_drawdown,
        "sharpe_like": sharpe_like,
        "equity_curve": equity_curve,
    }


def evaluate_predictions(
    y_true: list[int],
    y_score: list[float],
    *,
    threshold: float = 0.5,
    outcome_returns: list[float | None] | None = None,
) -> dict[str, Any]:
    predicted = [1 if score >= threshold else 0 for score in y_score]
    tp = sum(1 for truth, pred in zip(y_true, predicted) if truth == 1 and pred == 1)
    fp = sum(1 for truth, pred in zip(y_true, predicted) if truth == 0 and pred == 1)
    fn = sum(1 for truth, pred in zip(y_true, predicted) if truth == 1 and pred == 0)
    tn = sum(1 for truth, pred in zip(y_true, predicted) if truth == 0 and pred == 0)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    accuracy = (tp + tn) / len(y_true) if y_true else 0.0
    auc = _binary_auc(y_true, y_score)

    normalized_returns = outcome_returns or [_coerce_trade_return(None, label=value) for value in y_true]
    selected_returns = [
        float(value)
        for pred, value in zip(predicted, normalized_returns)
        if pred == 1 and value is not None
    ]
    trade_metrics = compute_trade_metrics(selected_returns)
    return {
        "rows": len(y_true),
        "threshold": threshold,
        "auc": auc,
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
        "winrate": trade_metrics["win_rate"],
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "profit_factor": trade_metrics["profit_factor"],
        "expectancy": trade_metrics["expectancy"],
        "average_trade_return": trade_metrics["average_trade_return"],
        "max_drawdown": trade_metrics["max_drawdown"],
        "sharpe_like": trade_metrics["sharpe_like"],
        "turnover": trade_metrics["turnover"],
        "trade_count": trade_metrics["trade_count"],
        "equity_curve": trade_metrics["equity_curve"],
    }


def calibrate_threshold(
    y_true: list[int],
    y_score: list[float],
    *,
    outcome_returns: list[float | None] | None = None,
    min_precision: float = 0.0,
) -> dict[str, Any]:
    if not y_score:
        return {"threshold": 0.5, "metrics": evaluate_predictions([], [], threshold=0.5)}

    candidate_thresholds = sorted({round(score, 3) for score in y_score} | {0.4, 0.5, 0.6})
    best_threshold = 0.5
    best_metrics = evaluate_predictions(y_true, y_score, threshold=best_threshold, outcome_returns=outcome_returns)
    best_objective = (
        float(best_metrics.get("expectancy") or 0.0),
        float(best_metrics.get("profit_factor") or 0.0),
        float(best_metrics.get("precision") or 0.0),
    )
    for threshold in candidate_thresholds:
        metrics = evaluate_predictions(y_true, y_score, threshold=threshold, outcome_returns=outcome_returns)
        if float(metrics.get("precision") or 0.0) < min_precision:
            continue
        objective = (
            float(metrics.get("expectancy") or 0.0),
            float(metrics.get("profit_factor") or 0.0),
            float(metrics.get("precision") or 0.0),
        )
        if objective > best_objective:
            best_threshold = threshold
            best_metrics = metrics
            best_objective = objective
    return {"threshold": best_threshold, "metrics": best_metrics}


def promotion_thresholds_pass(settings: Any, metrics: dict[str, Any]) -> tuple[bool, dict[str, float | None]]:
    auc = metrics.get("auc")
    precision = metrics.get("precision")
    profit_factor = metrics.get("profit_factor")
    expectancy = metrics.get("expectancy")
    max_drawdown = metrics.get("max_drawdown")
    winrate = metrics.get("winrate") or 0.0
    passed = (
        auc is not None
        and float(auc) >= settings.ml_promotion_min_auc
        and precision is not None
        and float(precision) >= settings.ml_promotion_min_precision
        and profit_factor is not None
        and float(profit_factor) >= settings.ml_promotion_min_profit_factor
        and expectancy is not None
        and float(expectancy) >= settings.ml_promotion_min_expectancy
        and max_drawdown is not None
        and float(max_drawdown) <= settings.ml_promotion_max_drawdown
        and float(winrate) >= settings.ml_promotion_min_winrate_lift
    )
    return (
        passed,
        {
            "auc": float(auc) if auc is not None else None,
            "precision": float(precision) if precision is not None else None,
            "profit_factor": float(profit_factor) if profit_factor is not None else None,
            "expectancy": float(expectancy) if expectancy is not None else None,
            "max_drawdown": float(max_drawdown) if max_drawdown is not None else None,
            "winrate": float(winrate),
        },
    )


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


def evaluate_rows(
    rows: list[dict[str, Any]],
    scores: list[float],
    *,
    threshold: float,
) -> dict[str, Any]:
    labels = [int(row["label"]) for row in rows]
    outcome_returns = _extract_outcome_returns(rows, labels)
    return evaluate_predictions(labels, scores, threshold=threshold, outcome_returns=outcome_returns)
