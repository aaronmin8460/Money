from __future__ import annotations

import json
from pathlib import Path

from app.config.settings import Settings
from app.domain.models import AssetClass
from app.execution.execution_service import ExecutionService
from app.ml.registry import initialize_registry, load_registry, promote_candidate, update_candidate
from app.ml.schema import ModelScoreResult
from app.ml.training import train_model
from app.news.feature_store import NewsFeatureStore
from app.news.llm_analysis import analyze_headlines
from app.news.rss_ingest import NewsHeadline, fetch_rss_headlines
from app.news.ticker_mapper import group_headlines_by_symbol
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.rl import ReplayTradingEnv
from app.services.auto_trader import AutoTrader
from app.services.broker import PaperBroker
from app.services.market_data import CSVMarketDataService
from app.strategies.base import Signal, TradeSignal


def _build_execution(tmp_path: Path) -> ExecutionService:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        log_dir=str(tmp_path / "logs"),
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    market_data = CSVMarketDataService()
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    return ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=True,
        market_data_service=market_data,
        settings=settings,
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            rows.append(json.loads(raw))
    return rows


def test_structured_outcome_logs_are_written(tmp_path: Path) -> None:
    execution = _build_execution(tmp_path)
    result = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="test_strategy",
            price=100.0,
            stop_price=95.0,
            reason="structured logging test",
        )
    )

    assert result["action"] == "dry_run"
    signal_rows = _read_jsonl(tmp_path / "logs" / "signals.jsonl")
    order_rows = _read_jsonl(tmp_path / "logs" / "orders.jsonl")
    outcome_rows = _read_jsonl(tmp_path / "logs" / "outcomes.jsonl")

    assert signal_rows
    assert order_rows
    assert outcome_rows
    assert outcome_rows[-1]["classification"] == "dry_run"
    assert outcome_rows[-1]["feature_snapshot"]["label_source"] in {"execution_outcome", "reward_risk_bootstrap"}


def test_ml_inference_hook_returns_score_and_applies_threshold_gating() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        ml_enabled=True,
        ml_min_score_threshold=0.7,
    )
    trader = AutoTrader(settings)

    class StubScorer:
        def score_signal(self, *args, **kwargs):
            return ModelScoreResult(
                enabled=True,
                score=0.4,
                threshold=0.7,
                passed=False,
                model_type="stub",
                reason="below_threshold",
            )

    trader.ml_scorer = StubScorer()
    signal = TradeSignal(
        symbol="AAPL",
        signal=Signal.BUY,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=100.0,
        stop_price=95.0,
        reason="ml gate test",
    )

    gated = trader._apply_ml_score_filter(signal, regime_snapshot={"bullish": 3}, news_features=None)

    assert gated.signal == Signal.HOLD
    assert gated.metrics["decision_code"] == "skipped_low_ml_score"
    assert gated.metrics["ml"]["score"] == 0.4


def test_sparse_data_training_path_fails_safely() -> None:
    result = train_model(
        [{"symbol": "AAPL", "signal": "BUY", "label": 1}],
        model_type="logistic_regression",
        min_train_rows=5,
    )

    assert result["trained"] is False
    assert "Not enough labeled rows" in result["reason"]


def test_registry_json_initializes_updates_and_promotes(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    current_model_path = tmp_path / "current_model.joblib"
    candidate_model_path = tmp_path / "candidate_model.joblib"
    candidate_model_path.write_text("candidate", encoding="utf-8")

    initialize_registry(registry_path)
    update_candidate(
        registry_path,
        model_path=str(candidate_model_path),
        model_type="logistic_regression",
        feature_version="v1",
        train_rows=100,
        validation_rows=20,
        metrics={"auc": 0.7, "precision": 0.6, "winrate": 0.6},
        notes="candidate ready",
    )
    promote_candidate(
        registry_path,
        current_model_path=current_model_path,
        candidate_model_path=candidate_model_path,
        notes="promoted",
    )

    registry = load_registry(registry_path)
    assert registry["promoted"] is True
    assert registry["current_model"]["path"] == str(current_model_path)
    assert current_model_path.read_text(encoding="utf-8") == "candidate"


def test_news_pipeline_works_without_openai_key(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        log_dir=str(tmp_path / "logs"),
        news_features_enabled=True,
        news_rss_enabled=True,
        openai_api_key=None,
    )

    class FakeParser:
        @staticmethod
        def parse(_url: str):
            return type(
                "ParsedFeed",
                (),
                {
                    "feed": type("Feed", (), {"title": "Unit Test Feed"})(),
                    "entries": [
                        type(
                            "Entry",
                            (),
                            {
                                "title": "$AAPL surges after earnings beat",
                                "summary": "AAPL posted strong growth.",
                                "link": "https://example.com/aapl",
                                "published": "Fri, 10 Apr 2026 14:05:00 GMT",
                            },
                        )()
                    ],
                },
            )()

    headlines = fetch_rss_headlines(lookback_hours=24, parser=FakeParser())
    grouped = group_headlines_by_symbol(headlines, known_symbols=["AAPL"], max_headlines_per_symbol=5)
    analysis = analyze_headlines("AAPL", grouped["AAPL"], settings=settings)
    store = NewsFeatureStore(settings)
    store.write_feature({**analysis, "headlines": [headline.to_dict() for headline in grouped["AAPL"]]})
    latest = store.latest_for_symbol("AAPL")

    assert analysis["analysis_mode"] == "heuristic"
    assert latest is not None
    assert latest["symbol"] == "AAPL"


def test_news_pipeline_with_mocked_openai_response_stores_feature_output(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        log_dir=str(tmp_path / "logs"),
        news_features_enabled=True,
        news_rss_enabled=True,
        news_llm_enabled=True,
        openai_api_key="test-key",
    )
    headlines = [
        NewsHeadline(
            title="$AAPL launches new product",
            summary="New product launch drives interest.",
            source="Unit Test",
            url="https://example.com/aapl-product",
            published_at="2026-04-10T14:05:00Z",
        )
    ]

    class FakeResponses:
        @staticmethod
        def create(**kwargs):
            _ = kwargs
            return type(
                "Response",
                (),
                {
                    "output_text": json.dumps(
                        {
                            "summary": "Product launch appears constructive.",
                            "sentiment_label": "positive",
                            "sentiment_score": 0.82,
                            "risk_tags": ["product_execution"],
                            "relevance_score": 0.91,
                        }
                    )
                },
            )()

    class FakeClient:
        responses = FakeResponses()

    analysis = analyze_headlines("AAPL", headlines, settings=settings, client=FakeClient())
    store = NewsFeatureStore(settings)
    store.write_feature(analysis)
    latest = store.latest_for_symbol("AAPL")

    assert analysis["analysis_mode"] == "llm"
    assert latest is not None
    assert latest["sentiment_label"] == "positive"
    assert latest["relevance_score"] == 0.91


def test_rl_module_is_importable_and_isolated_from_live_path(tmp_path: Path) -> None:
    env = ReplayTradingEnv(history=[{"close": 100.0}, {"close": 101.0}])
    first = env.reset()
    step = env.step(action=1)

    execution = _build_execution(tmp_path)
    assert first["close"] == 100.0
    assert step.info["experimental_only"] is True
    assert not hasattr(execution, "rl_policy")
