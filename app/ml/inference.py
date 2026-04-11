from __future__ import annotations

from typing import Any

from app.config.settings import Settings, get_settings
from app.ml.features import build_signal_feature_row
from app.ml.preprocessing import model_uses_internal_preprocessing, prepare_feature_frame
from app.ml.registry import load_registry
from app.ml.schema import ModelScoreResult
from app.ml.training import load_model_bundle
from app.monitoring.logger import get_logger
from app.strategies.base import Signal, TradeSignal

logger = get_logger("ml.inference")


class SignalScorer:
    def __init__(self, settings: Settings | None = None, model_bundle: dict[str, Any] | None = None):
        self.settings = settings or get_settings()
        self._model_bundle = model_bundle

    def _load_bundle(self) -> dict[str, Any] | None:
        if self._model_bundle is not None:
            return self._model_bundle
        registry = load_registry(self.settings.ml_registry_path)
        current = registry.get("current_model") or {}
        model_path = current.get("path") or self.settings.ml_current_model_path
        try:
            self._model_bundle = load_model_bundle(model_path)
        except FileNotFoundError:
            logger.info("No current ML model found; skipping ML scoring", extra={"model_path": model_path})
            self._model_bundle = None
        except Exception as exc:
            logger.warning("Failed to load ML model bundle: %s", exc)
            self._model_bundle = None
        return self._model_bundle

    def score_signal(
        self,
        signal: TradeSignal,
        *,
        market_overview: dict[str, Any] | None = None,
        news_features: dict[str, Any] | None = None,
        latest_price: float | None = None,
    ) -> ModelScoreResult:
        if not self.settings.ml_enabled:
            return ModelScoreResult(enabled=False, score=None, threshold=None, passed=True, reason="ml_disabled")
        if signal.signal != Signal.BUY:
            return ModelScoreResult(
                enabled=True,
                score=None,
                threshold=self.settings.ml_min_score_threshold,
                passed=True,
                model_type=None,
                reason="non_buy_signal",
            )

        bundle = self._load_bundle()
        if not bundle:
            return ModelScoreResult(
                enabled=True,
                score=None,
                threshold=self.settings.ml_min_score_threshold,
                passed=True,
                model_type=None,
                reason="model_unavailable",
            )

        feature_row = build_signal_feature_row(
            signal,
            cycle_id=str((signal.metrics or {}).get("cycle_id") or ""),
            latest_price=latest_price,
            market_overview=market_overview,
            news_features=news_features,
        )
        prepared = prepare_feature_frame([feature_row], model_bundle=bundle)
        scoring_frame = prepared.frame
        model = bundle["model"]
        if not model_uses_internal_preprocessing(model):
            scoring_frame = prepare_feature_frame(
                [feature_row],
                model_bundle=bundle,
                fill_missing_numeric=0.0,
            ).frame
        try:
            score = float(model.predict_proba(scoring_frame)[0][1])
        except Exception as exc:
            logger.warning(
                "ML inference failed for candidate; skipping trade candidate conservatively",
                extra={
                    "symbol": signal.symbol,
                    "strategy": signal.strategy_name,
                    "cycle_id": str((signal.metrics or {}).get("cycle_id") or ""),
                    "model_type": str(bundle.get("model_type")),
                    "missing_numeric_features": prepared.missing_numeric_features,
                    "feature_version": str(bundle.get("feature_version") or ""),
                    "error": str(exc),
                },
            )
            return ModelScoreResult(
                enabled=True,
                score=None,
                threshold=self.settings.ml_min_score_threshold,
                passed=False,
                model_type=str(bundle.get("model_type")),
                reason="ml_inference_error",
            )
        threshold = float(self.settings.ml_min_score_threshold)
        return ModelScoreResult(
            enabled=True,
            score=score,
            threshold=threshold,
            passed=score >= threshold,
            model_type=str(bundle.get("model_type")),
            reason="score_above_threshold" if score >= threshold else "below_threshold",
        )
