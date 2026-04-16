from __future__ import annotations

import importlib
import json
import math
import sys
import types
from pathlib import Path

from sklearn.base import BaseEstimator, ClassifierMixin

from app.config.settings import Settings
from app.domain.models import AssetClass
from app.ml.evaluation import predict_scores, promotion_thresholds_pass, walk_forward_split
from app.ml.features import build_signal_feature_row
from app.ml import model_selection as model_selection_module
from app.ml.inference import SignalScorer
from app.ml.model_selection import ProbabilityAveragingEnsemble
from app.ml.registry import load_registry, update_candidate
from app.ml.training import load_model_bundle, save_model_bundle, train_model
from app.monitoring.outcome_logger import derive_bootstrap_label
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


class _ConstantProbabilityEstimator(ClassifierMixin, BaseEstimator):
    def __init__(self, probability: float = 0.5):
        self.probability = probability

    def fit(self, frame: object, labels: list[int]) -> "_ConstantProbabilityEstimator":
        self.classes_ = [0, 1]
        return self

    def predict_proba(self, frame: object) -> list[list[float]]:
        shape = getattr(frame, "shape", None)
        row_count = int(shape[0]) if shape is not None else len(frame)
        return [[1.0 - self.probability, self.probability] for _ in range(row_count)]


class _FakeXGBClassifier(_ConstantProbabilityEstimator):
    def __init__(
        self,
        n_estimators: int = 50,
        max_depth: int = 3,
        learning_rate: float = 0.1,
        subsample: float = 0.9,
        colsample_bytree: float = 0.9,
        eval_metric: str | None = None,
        random_state: int | None = None,
    ):
        super().__init__(probability=0.8)
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.subsample = subsample
        self.colsample_bytree = colsample_bytree
        self.eval_metric = eval_metric
        self.random_state = random_state


class _FakeLightGBMClassifier(_ConstantProbabilityEstimator):
    def __init__(
        self,
        n_estimators: int = 50,
        max_depth: int = 3,
        learning_rate: float = 0.1,
        subsample: float = 0.9,
        colsample_bytree: float = 0.9,
        random_state: int | None = None,
        verbosity: int = -1,
    ):
        super().__init__(probability=0.2)
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.subsample = subsample
        self.colsample_bytree = colsample_bytree
        self.random_state = random_state
        self.verbosity = verbosity


def _install_fake_boosted_model_modules(monkeypatch) -> None:
    xgboost_module = types.ModuleType("xgboost")
    xgboost_module.XGBClassifier = _FakeXGBClassifier
    lightgbm_module = types.ModuleType("lightgbm")
    lightgbm_module.LGBMClassifier = _FakeLightGBMClassifier
    monkeypatch.setitem(sys.modules, "xgboost", xgboost_module)
    monkeypatch.setitem(sys.modules, "lightgbm", lightgbm_module)


def test_training_handles_none_and_nan_optional_numeric_features() -> None:
    rows = _training_rows()
    rows[0]["atr"] = None
    rows[1]["momentum"] = float("nan")
    rows[2]["scanner_signal_quality"] = None
    rows[3]["quote_age_seconds"] = float("nan")

    result = train_model(rows, model_type="logistic_regression", min_train_rows=10)

    assert result["trained"] is True
    assert result["bundle"] is not None


def test_training_selects_lightgbm_when_available(monkeypatch) -> None:
    _install_fake_boosted_model_modules(monkeypatch)

    result = train_model(_training_rows(), model_type="lightgbm", min_train_rows=10)

    assert result["trained"] is True
    assert result["bundle"]["model_type"] == "lightgbm"
    assert result["bundle"]["requested_model_type"] == "lightgbm"
    assert result["bundle"]["base_estimator_class"] == "_FakeLightGBMClassifier"
    assert result["bundle"]["model_selection"]["resolved_model_type"] == "lightgbm"
    assert result["bundle"]["model_selection"]["used_fallback"] is False
    assert isinstance(result["bundle"]["model"].named_steps["model"], _FakeLightGBMClassifier)


def test_training_selects_ensemble_and_records_components(monkeypatch) -> None:
    _install_fake_boosted_model_modules(monkeypatch)

    result = train_model(_training_rows(), model_type="ensemble", min_train_rows=10)

    assert result["trained"] is True
    assert result["bundle"]["model_type"] == "ensemble"
    assert result["bundle"]["component_model_types"] == ["logistic_regression", "xgboost", "lightgbm"]
    ensemble = result["bundle"]["model"].named_steps["model"]
    assert isinstance(ensemble, ProbabilityAveragingEnsemble)
    assert ensemble.component_model_types == ["logistic_regression", "xgboost", "lightgbm"]
    assert result["bundle"]["model_selection"]["resolved_model_type"] == "ensemble"
    assert result["bundle"]["model_selection"]["used_fallback"] is False


def test_probability_averaging_ensemble_averages_component_probabilities() -> None:
    ensemble = ProbabilityAveragingEnsemble(
        [
            ("logistic_regression", _ConstantProbabilityEstimator(0.2)),
            ("xgboost", _ConstantProbabilityEstimator(0.5)),
            ("lightgbm", _ConstantProbabilityEstimator(0.8)),
        ]
    )
    ensemble.fit([[0.0], [1.0]], [0, 1])

    scores = ensemble.predict_proba([[0.0], [1.0]])

    assert len(scores) == 2
    assert math.isclose(float(scores[0][1]), 0.5)
    assert math.isclose(float(scores[1][1]), 0.5)


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


def test_hold_with_tracked_position_routes_to_exit_model_purpose() -> None:
    signal = TradeSignal(
        symbol="AAPL",
        signal=Signal.HOLD,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=100.0,
        metrics={
            "has_tracked_position": True,
            "has_sellable_long_position": True,
        },
    )

    row = build_signal_feature_row(signal)

    assert row.model_purpose == "exit"


def test_hold_without_tracked_position_stays_entry_model_purpose() -> None:
    signal = TradeSignal(
        symbol="AAPL",
        signal=Signal.HOLD,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=100.0,
        metrics={},
    )

    row = build_signal_feature_row(signal)

    assert row.model_purpose == "entry"


def test_reduce_only_sell_signal_stays_exit_model_purpose() -> None:
    signal = TradeSignal(
        symbol="AAPL",
        signal=Signal.SELL,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=103.0,
        reduce_only=True,
        exit_stage="tp1",
    )

    row = build_signal_feature_row(signal)

    assert row.model_purpose == "exit"


def test_exit_hold_examples_remain_unlabeled_without_realized_history() -> None:
    signal = TradeSignal(
        symbol="AAPL",
        signal=Signal.HOLD,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=100.0,
        metrics={"has_tracked_position": True},
    )
    feature_row = build_signal_feature_row(signal)

    label, source = derive_bootstrap_label(
        signal=signal,
        action="hold",
        classification="hold",
        feature_snapshot=feature_row,
    )

    assert feature_row.model_purpose == "exit"
    assert label is None
    assert source is None


def test_authoritative_exit_paths_are_labeled_positive_for_exit_research() -> None:
    signal = TradeSignal(
        symbol="AAPL",
        signal=Signal.SELL,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=94.0,
        reduce_only=True,
        exit_stage="stop",
        metrics={
            "exit_state": {"unrealized_return": -0.05},
            "has_tracked_position": True,
            "has_sellable_long_position": True,
        },
    )
    feature_row = build_signal_feature_row(signal)

    label, source = derive_bootstrap_label(
        signal=signal,
        action="submitted",
        classification="submitted",
        feature_snapshot=feature_row,
    )

    assert feature_row.model_purpose == "exit"
    assert label == 1
    assert source == "exit_authoritative_bootstrap"


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


def test_lightgbm_bundle_save_and_load_preserves_model_type(monkeypatch, tmp_path: Path) -> None:
    _install_fake_boosted_model_modules(monkeypatch)

    result = train_model(_training_rows(), model_type="lightgbm", min_train_rows=10)
    assert result["trained"] is True

    bundle_path = tmp_path / "candidate_model.joblib"
    save_model_bundle(result["bundle"], bundle_path)
    loaded_bundle = load_model_bundle(bundle_path)

    assert loaded_bundle["model_type"] == "lightgbm"
    assert loaded_bundle["requested_model_type"] == "lightgbm"
    assert loaded_bundle["base_estimator_class"] == "_FakeLightGBMClassifier"
    assert loaded_bundle["model_selection"]["resolved_model_type"] == "lightgbm"

    scores = predict_scores(loaded_bundle, _training_rows()[:2])

    assert len(scores) == 2
    assert all(0.0 <= score <= 1.0 for score in scores)


def test_logistic_regression_bundle_metadata_stays_logistic(tmp_path: Path) -> None:
    result = train_model(_training_rows(), model_type="logistic_regression", min_train_rows=10)
    assert result["trained"] is True

    bundle_path = tmp_path / "candidate_model.joblib"
    save_model_bundle(result["bundle"], bundle_path)
    loaded_bundle = load_model_bundle(bundle_path)

    assert loaded_bundle["model_type"] == "logistic_regression"
    assert loaded_bundle["requested_model_type"] == "logistic_regression"
    assert loaded_bundle["base_estimator_class"] == "LogisticRegression"
    assert loaded_bundle["model_selection"]["used_fallback"] is False


def test_lightgbm_fallback_is_explicit_in_bundle_registry_and_logs(
    monkeypatch,
    caplog,
    tmp_path: Path,
) -> None:
    def fail_lightgbm_import() -> object:
        raise OSError("libomp missing")

    monkeypatch.setattr(model_selection_module, "_build_lightgbm_estimator", fail_lightgbm_import)
    caplog.set_level("WARNING", logger="ml.training")

    result = train_model(_training_rows(), model_type="lightgbm", min_train_rows=10)

    assert result["trained"] is True
    bundle = result["bundle"]
    assert bundle["model_type"] == "logistic_regression"
    assert bundle["requested_model_type"] == "lightgbm"
    assert bundle["base_estimator_class"] == "LogisticRegression"
    assert bundle["model_selection"]["used_fallback"] is True
    assert bundle["model_selection"]["resolved_model_type"] == "logistic_regression"
    assert "libomp missing" in bundle["model_selection"]["fallback_reason"]
    assert "falling back to the resolved estimator" in caplog.text

    bundle_path = tmp_path / "candidate_model.joblib"
    save_model_bundle(bundle, bundle_path)
    loaded_bundle = load_model_bundle(bundle_path)

    update_candidate(
        tmp_path / "registry.json",
        model_path=str(bundle_path),
        model_type=loaded_bundle["model_type"],
        requested_model_type=loaded_bundle["requested_model_type"],
        base_estimator_class=loaded_bundle["base_estimator_class"],
        model_selection=loaded_bundle["model_selection"],
        feature_version=loaded_bundle["feature_version"],
        train_rows=result["train_rows"],
        validation_rows=result["validation_rows"],
        metrics=result["metrics"],
        trading_metrics={},
        notes="fallback regression test",
    )
    registry = load_registry(tmp_path / "registry.json")
    candidate = registry["models"]["entry"]["candidate_model"]

    assert candidate["model_type"] == "logistic_regression"
    assert candidate["requested_model_type"] == "lightgbm"
    assert candidate["model_selection"]["used_fallback"] is True
    assert "libomp missing" in candidate["model_selection"]["fallback_reason"]


def test_training_save_writes_compact_shap_feature_artifact(monkeypatch, tmp_path: Path) -> None:
    class FakeLinearExplainer:
        def __init__(self, estimator: object, background: object) -> None:
            self.estimator = estimator
            self.background = background

        def shap_values(self, sample: object) -> list[list[float]]:
            shape = getattr(sample, "shape")
            row_count = int(shape[0])
            column_count = int(shape[1])
            return [[float(index + 1) for index in range(column_count)] for _ in range(row_count)]

    fake_shap_module = types.ModuleType("shap")
    fake_shap_module.LinearExplainer = FakeLinearExplainer
    monkeypatch.setitem(sys.modules, "shap", fake_shap_module)

    result = train_model(_training_rows(), model_type="logistic_regression", min_train_rows=10)
    assert result["trained"] is True
    assert result["bundle"]["feature_importance"]["status"] == "available"

    bundle_path = tmp_path / "candidate_model.joblib"
    save_model_bundle(result["bundle"], bundle_path)

    artifact_payload = json.loads((tmp_path / "shap_values.json").read_text(encoding="utf-8"))
    entry_payload = artifact_payload["models"]["entry"]
    assert artifact_payload["status"] == "available"
    assert entry_payload["source"] == "shap_mean_abs"
    assert 0 < len(entry_payload["top_features"]) <= 20


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


def test_train_script_records_lightgbm_candidate_metadata(monkeypatch, tmp_path: Path) -> None:
    _install_fake_boosted_model_modules(monkeypatch)
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    sys.modules.pop("_bootstrap", None)
    sys.modules.pop("train_model", None)
    train_model_script = importlib.import_module("train_model")
    dataset_path = tmp_path / "training_data.jsonl"
    dataset_path.write_text(
        "\n".join(json.dumps(row) for row in _training_rows()),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        ml_model_type="lightgbm",
        ml_min_train_rows=10,
        model_dir=str(tmp_path),
        ml_registry_path=str(tmp_path / "registry.json"),
        ml_entry_candidate_model_path=str(tmp_path / "candidate_model.joblib"),
        ml_exit_candidate_model_path=str(tmp_path / "candidate_exit_model.joblib"),
    )
    monkeypatch.setattr(train_model_script, "get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["train_model.py", "--dataset", str(dataset_path), "--purpose", "entry"])

    train_model_script.main()

    bundle = load_model_bundle(settings.ml_entry_candidate_model_path)
    registry = load_registry(settings.ml_registry_path)
    candidate = registry["models"]["entry"]["candidate_model"]

    assert bundle["model_type"] == "lightgbm"
    assert bundle["requested_model_type"] == "lightgbm"
    assert bundle["base_estimator_class"] == "_FakeLightGBMClassifier"
    assert candidate["model_type"] == "lightgbm"
    assert candidate["requested_model_type"] == "lightgbm"
    assert candidate["model_selection"]["resolved_model_type"] == "lightgbm"
    assert candidate["model_selection"]["used_fallback"] is False


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


def test_promotion_gating_enforces_winrate_lift_when_current_metrics_exist() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        ml_promotion_min_auc=0.6,
        ml_promotion_min_precision=0.55,
        ml_promotion_min_profit_factor=1.1,
        ml_promotion_min_expectancy=0.01,
        ml_promotion_max_drawdown=0.2,
        ml_promotion_min_winrate_lift=0.03,
    )
    candidate_metrics = {
        "auc": 0.7,
        "precision": 0.6,
        "profit_factor": 1.4,
        "expectancy": 0.02,
        "max_drawdown": 0.1,
        "winrate": 0.61,
    }

    assert promotion_thresholds_pass(settings, candidate_metrics, current_metrics={"winrate": 0.57})[0] is True
    assert promotion_thresholds_pass(settings, candidate_metrics, current_metrics={"winrate": 0.59})[0] is False


def test_sparse_unlabeled_exit_dataset_fails_safely() -> None:
    result = train_model(
        [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "strategy_name": "test_strategy",
                "signal": "HOLD",
                "direction": "long",
                "model_purpose": "exit",
                "label": None,
            },
            {
                "symbol": "MSFT",
                "asset_class": "equity",
                "strategy_name": "test_strategy",
                "signal": "SELL",
                "direction": "long",
                "model_purpose": "exit",
                "label": None,
            },
        ],
        model_type="logistic_regression",
        min_train_rows=1,
        purpose="exit",
    )

    assert result["trained"] is False


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
