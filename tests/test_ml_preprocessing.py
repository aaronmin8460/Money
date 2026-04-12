from __future__ import annotations

import math
from pathlib import Path

from app.config.settings import Settings
from app.domain.models import AssetClass
from app.ml.evaluation import predict_scores, promotion_thresholds_pass, walk_forward_split
from app.ml.inference import SignalScorer
from app.ml.registry import load_registry, update_candidate
from app.ml.training import load_model_bundle, save_model_bundle, train_model
from app.services.auto_trader import AutoTrader
from app.strategies.base import Signal, TradeSignal


def _training_rows() -> list[dict[str, object]]:
    return [
        {
            "symbol": "AAPL",
            "asset_class": "equity",
            "strategy_name": "test_strategy",
            "signal": "BUY",
            "direction": "long",
            "regime": "bull",
            "session_state": "open",
            "price_source_used": "snapshot",
            "news_sentiment_label": "positive",
            "confidence": 0.8,
            "entry": 100.0,
            "stop": 95.0,
            "target": 108.0,
            "atr": 2.0,
            "momentum": 0.7,
            "liquidity": 0.8,
            "spread": 0.01,
            "latest_price": 101.0,
            "latest_volume": 100000.0,
            "avg_volume": 80000.0,
            "dollar_volume": 8000000.0,
            "scanner_signal_quality": 0.9,
            "quote_age_seconds": 1.0,
            "market_bullish_count": 4.0,
            "market_bearish_count": 1.0,
            "news_sentiment_score": 0.6,
            "news_relevance_score": 0.9,
            "news_risk_tags_count": 1.0,
            "label": 1,
        },
        {
            "symbol": "MSFT",
            "asset_class": "equity",
            "strategy_name": "test_strategy",
            "signal": "BUY",
            "direction": "long",
            "regime": "bear",
            "session_state": "open",
            "price_source_used": "snapshot",
            "news_sentiment_label": "negative",
            "confidence": 0.3,
            "entry": 100.0,
            "stop": 99.0,
            "target": 101.0,
            "atr": 1.0,
            "momentum": 0.2,
            "liquidity": 0.4,
            "spread": 0.03,
            "latest_price": 99.5,
            "latest_volume": 50000.0,
            "avg_volume": 40000.0,
            "dollar_volume": 4000000.0,
            "scanner_signal_quality": 0.2,
            "quote_age_seconds": 3.0,
            "market_bullish_count": 1.0,
            "market_bearish_count": 4.0,
            "news_sentiment_score": -0.6,
            "news_relevance_score": 0.5,
            "news_risk_tags_count": 2.0,
            "label": 0,
        },
    ] * 30


def test_training_handles_none_and_nan_optional_numeric_features() -> None:
    rows = _training_rows()
    rows[0]["atr"] = None
    rows[1]["momentum"] = float("nan")
    rows[2]["scanner_signal_quality"] = None
    rows[3]["quote_age_seconds"] = float("nan")

    result = train_model(rows, model_type="logistic_regression", min_train_rows=10)

    assert result["trained"] is True
    assert result["bundle"] is not None


def test_inference_handles_none_and_nan_optional_numeric_features() -> None:
    result = train_model(_training_rows(), model_type="logistic_regression", min_train_rows=10)
    assert result["trained"] is True

    scores = predict_scores(
        result["bundle"],
        [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "strategy_name": "test_strategy",
                "signal": "BUY",
                "direction": "long",
                "entry": None,
                "stop": None,
                "target": None,
                "latest_price": None,
                "scanner_signal_quality": float("nan"),
                "news_sentiment_score": "nan",
            },
            {
                "symbol": "MSFT",
                "asset_class": "equity",
                "strategy_name": "test_strategy",
                "signal": "BUY",
                "direction": "long",
            },
        ],
    )

    assert len(scores) == 2
    assert all(0.0 <= score <= 1.0 for score in scores)


def test_signal_scorer_does_not_crash_on_buy_candidate_with_missing_features() -> None:
    result = train_model(_training_rows(), model_type="logistic_regression", min_train_rows=10)
    assert result["trained"] is True

    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False, ml_enabled=True)
    scorer = SignalScorer(settings=settings, model_bundle=result["bundle"])
    signal = TradeSignal(
        symbol="AAPL",
        signal=Signal.BUY,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=100.0,
        stop_price=95.0,
        reason="missing features should impute",
        momentum_score=float("nan"),
        liquidity_score=None,
        metrics={
            "cycle_id": "cycle-1",
            "avg_volume": None,
            "latest_volume": float("nan"),
            "scan_signal_quality_score": None,
            "quote_age_seconds": float("nan"),
            "spread_pct": float("nan"),
        },
    )

    scored = scorer.score_signal(signal, market_overview={"bullish": 3}, news_features=None)

    assert scored.enabled is True
    assert scored.reason in {"score_above_threshold", "below_threshold"}
    assert scored.score is not None


def test_ml_inference_error_does_not_crash_auto_trader_buy_candidate() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        ml_enabled=True,
        ml_min_score_threshold=0.55,
    )
    trader = AutoTrader(settings)

    class BrokenModel:
        def predict_proba(self, _frame):
            raise ValueError("Input X contains NaN.")

    broken_bundle = {
        "model": BrokenModel(),
        "model_type": "logistic_regression",
        "feature_version": "v1",
        "categorical_features": [
            "symbol",
            "asset_class",
            "strategy_name",
            "signal",
            "direction",
            "regime",
            "session_state",
            "price_source_used",
            "news_sentiment_label",
        ],
        "numeric_features": [
            "confidence",
            "entry",
            "stop",
            "target",
            "atr",
            "momentum",
            "liquidity",
            "spread",
            "latest_price",
            "latest_volume",
            "avg_volume",
            "dollar_volume",
            "scanner_signal_quality",
            "quote_age_seconds",
            "market_bullish_count",
            "market_bearish_count",
            "news_sentiment_score",
            "news_relevance_score",
            "news_risk_tags_count",
        ],
        "threshold": 0.55,
    }
    trader.ml_scorer = SignalScorer(settings=settings, model_bundle=broken_bundle)
    signal = TradeSignal(
        symbol="AAPL",
        signal=Signal.BUY,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=100.0,
        stop_price=95.0,
        reason="broken ml model should degrade safely",
        momentum_score=float("nan"),
        metrics={"cycle_id": "cycle-2", "avg_volume": None},
    )

    gated = trader._apply_ml_score_filter(signal, regime_snapshot={"bullish": 2}, news_features=None)

    assert gated.signal == Signal.HOLD
    assert gated.metrics["decision_code"] == "ml_inference_error"
    assert gated.metrics["ml"]["reason"] == "ml_inference_error"
    assert gated.metrics["ml"]["passed"] is False


def test_non_buy_signal_still_returns_non_buy_signal_reason() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False, ml_enabled=True)
    scorer = SignalScorer(settings=settings, model_bundle={"model": object()})
    signal = TradeSignal(
        symbol="BTC/USD",
        signal=Signal.HOLD,
        asset_class=AssetClass.CRYPTO,
        strategy_name="crypto_momentum_trend",
        price=70000.0,
    )

    scored = scorer.score_signal(signal, market_overview={"bearish": 1}, news_features=None)

    assert scored.enabled is True
    assert scored.reason == "non_buy_signal"
    assert scored.passed is True


def test_model_bundle_save_and_load_preserves_missing_value_safe_scoring(tmp_path: Path) -> None:
    result = train_model(_training_rows(), model_type="logistic_regression", min_train_rows=10)
    assert result["trained"] is True

    bundle_path = tmp_path / "bundle.joblib"
    save_model_bundle(result["bundle"], bundle_path)
    loaded_bundle = load_model_bundle(bundle_path)

    scores = predict_scores(
        loaded_bundle,
        [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "strategy_name": "test_strategy",
                "signal": "BUY",
                "direction": "long",
                "entry": None,
                "latest_price": 100.0,
                "avg_volume": None,
                "quote_age_seconds": float("nan"),
            }
        ],
    )

    assert len(scores) == 1
    assert math.isfinite(scores[0])


def test_entry_and_exit_model_loading_paths_are_independent(tmp_path: Path) -> None:
    result = train_model(_training_rows(), model_type="logistic_regression", min_train_rows=10)
    assert result["trained"] is True

    entry_bundle = {**result["bundle"], "purpose": "entry"}
    exit_bundle = {**result["bundle"], "purpose": "exit"}
    entry_path = tmp_path / "entry.joblib"
    exit_path = tmp_path / "exit.joblib"
    save_model_bundle(entry_bundle, entry_path)
    save_model_bundle(exit_bundle, exit_path)
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        ml_enabled=True,
        exit_model_enabled=True,
        ml_entry_current_model_path=str(entry_path),
        ml_exit_current_model_path=str(exit_path),
        ml_registry_path=str(tmp_path / "registry.json"),
    )

    scorer = SignalScorer(settings=settings)

    assert scorer._load_bundle("entry")["purpose"] == "entry"
    assert scorer._load_bundle("exit")["purpose"] == "exit"


def test_walk_forward_split_does_not_leak_future_rows() -> None:
    rows = [
        {"generated_at": f"2026-01-{day:02d}T00:00:00Z", "label": day % 2}
        for day in range(1, 11)
    ]

    training, validation = walk_forward_split(rows, validation_ratio=0.3)

    assert len(training) == 7
    assert len(validation) == 3
    assert max(row["generated_at"] for row in training) < min(row["generated_at"] for row in validation)


def test_promotion_gating_respects_ml_and_trading_thresholds() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        ml_promotion_min_auc=0.6,
        ml_promotion_min_precision=0.55,
        ml_promotion_min_profit_factor=1.1,
        ml_promotion_min_expectancy=0.01,
        ml_promotion_max_drawdown=0.2,
    )
    passing_metrics = {
        "auc": 0.7,
        "precision": 0.6,
        "profit_factor": 1.4,
        "expectancy": 0.02,
        "max_drawdown": 0.1,
        "winrate": 0.6,
    }
    failing_metrics = {**passing_metrics, "max_drawdown": 0.35}

    assert promotion_thresholds_pass(settings, passing_metrics)[0] is True
    assert promotion_thresholds_pass(settings, failing_metrics)[0] is False


def test_registry_stores_exit_candidate_metrics_separately(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"

    update_candidate(
        registry_path,
        model_path=str(tmp_path / "exit_candidate.joblib"),
        model_type="logistic_regression",
        feature_version="v2",
        train_rows=120,
        validation_rows=24,
        metrics={"auc": 0.66, "precision": 0.58},
        trading_metrics={"profit_factor": 1.2},
        notes="exit candidate",
        model_purpose="exit",
    )

    registry = load_registry(registry_path)

    assert registry["models"]["exit"]["candidate_model"]["metrics"]["auc"] == 0.66
    assert registry["models"]["exit"]["candidate_model"]["trading_metrics"]["profit_factor"] == 1.2


def test_sparse_dataset_fails_safely() -> None:
    result = train_model(_training_rows()[:4], model_type="logistic_regression", min_train_rows=10)

    assert result["trained"] is False
