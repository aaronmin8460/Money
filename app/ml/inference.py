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
        self._bundle_cache: dict[str, dict[str, Any] | None] = {}

    def _resolve_override_bundle(self, purpose: str) -> dict[str, Any] | None:
        if self._model_bundle is None:
            return None
        if "model" in self._model_bundle:
            return self._model_bundle
        nested_bundle = self._model_bundle.get(purpose)
        if isinstance(nested_bundle, dict) and "model" in nested_bundle:
            return nested_bundle
        return None

    def _resolve_bundle_path(self, purpose: str, registry: dict[str, Any]) -> str:
        models_registry = registry.get("models", {}) if isinstance(registry, dict) else {}
        purpose_registry = models_registry.get(purpose, {}) if isinstance(models_registry, dict) else {}
        current = purpose_registry.get("current_model") if isinstance(purpose_registry, dict) else None
        if purpose == "exit":
            return str((current or {}).get("path") or self.settings.ml_exit_current_model_path)
        return str(
            (current or {}).get("path")
            or (registry.get("current_model") or {}).get("path")
            or self.settings.ml_entry_current_model_path
            or self.settings.ml_current_model_path
        )

    def _load_bundle(self, purpose: str = "entry") -> dict[str, Any] | None:
        override_bundle = self._resolve_override_bundle(purpose)
        if override_bundle is not None:
            return override_bundle
        if purpose in self._bundle_cache:
            return self._bundle_cache[purpose]
        registry = load_registry(self.settings.ml_registry_path)
        model_path = self._resolve_bundle_path(purpose, registry)
        try:
            self._bundle_cache[purpose] = load_model_bundle(model_path)
        except FileNotFoundError:
            logger.info(
                "No current ML model found; skipping ML scoring",
                extra={"model_path": model_path, "purpose": purpose},
            )
            self._bundle_cache[purpose] = None
        except Exception as exc:
            logger.warning("Failed to load ML model bundle (%s): %s", purpose, exc)
            self._bundle_cache[purpose] = None
        return self._bundle_cache[purpose]

    def score_signal(
        self,
        signal: TradeSignal,
        *,
        market_overview: dict[str, Any] | None = None,
        news_features: dict[str, Any] | None = None,
        latest_price: float | None = None,
        purpose: str = "entry",
    ) -> ModelScoreResult:
        purpose = purpose.strip().lower()
        is_enabled = self.settings.ml_enabled and (
            self.settings.entry_model_enabled if purpose == "entry" else self.settings.exit_model_enabled
        )
        threshold = (
            float(self.settings.ml_min_score_threshold)
            if purpose == "entry"
            else float(self.settings.ml_exit_min_score)
        )
        if not is_enabled:
            return ModelScoreResult(
                enabled=False,
                score=None,
                threshold=threshold,
                passed=True,
                purpose=purpose,
                reason="ml_disabled",
            )
        if purpose == "entry" and signal.signal != Signal.BUY:
            return ModelScoreResult(
                enabled=True,
                score=None,
                threshold=threshold,
                passed=True,
                purpose=purpose,
                model_type=None,
                reason="non_buy_signal",
            )

        bundle = self._load_bundle(purpose)
        if not bundle:
            return ModelScoreResult(
                enabled=True,
                score=None,
                threshold=threshold,
                passed=True,
                purpose=purpose,
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
                "purpose": purpose,
                "missing_numeric_features": prepared.missing_numeric_features,
                "feature_version": str(bundle.get("feature_version") or ""),
                "error": str(exc),
            },
        )
            return ModelScoreResult(
                enabled=True,
                score=None,
                threshold=threshold,
                passed=False,
                purpose=purpose,
                model_type=str(bundle.get("model_type")),
                reason="ml_inference_error",
            )
        threshold = float(bundle.get("threshold") or threshold)
        return ModelScoreResult(
            enabled=True,
            score=score,
            threshold=threshold,
            passed=score >= threshold,
            purpose=purpose,
            model_type=str(bundle.get("model_type")),
            reason="score_above_threshold" if score >= threshold else "below_threshold",
        )

    def score_exit_signal(
        self,
        signal: TradeSignal,
        *,
        market_overview: dict[str, Any] | None = None,
        news_features: dict[str, Any] | None = None,
        latest_price: float | None = None,
    ) -> ModelScoreResult:
        return self.score_signal(
            signal,
            market_overview=market_overview,
            news_features=news_features,
            latest_price=latest_price,
            purpose="exit",
        )
