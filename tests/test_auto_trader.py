import tempfile
import uuid
from datetime import datetime, timezone

import pandas as pd

from app.services import auto_trader as auto_trader_module
from app.config.settings import Settings
from app.domain.models import AssetClass, AssetMetadata, MarketSessionStatus, NormalizedMarketSnapshot, QuoteSnapshot, SessionState
from app.portfolio.portfolio import Position
from app.services.auto_trader import AutoTrader
from app.services.scanner import ScanResult
from app.strategies.base import Signal, TradeSignal


def _test_lock_path() -> str:
    return f"{tempfile.gettempdir()}/money-auto-trader-{uuid.uuid4().hex}.lock"


def test_auto_trader_run_now_returns_result() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        default_symbols=["AAPL", "SPY"],
        max_positions=2,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    response = trader.run_now()

    assert response["success"] is True
    assert isinstance(response["results"], list)
    assert trader.get_status()["running"] is False


def test_auto_trader_prevents_duplicate_start() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    assert trader.start() is True
    assert trader.start() is False
    assert trader.stop() is True


def test_auto_trader_start_stop_send_notifications(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    calls: list[dict[str, str]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            calls.append(kwargs)
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            return True

        def diagnostics(self):
            return {}

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    assert trader.start() is True
    assert trader.stop() is True
    assert calls[0]["event"] == "Paper auto-trader started"
    assert calls[0]["reason"] == "background loop started"
    assert calls[1]["event"] == "Paper auto-trader stopped"
    assert calls[1]["reason"] == "background loop stopped"


def test_scan_summary_sends_once_per_cycle(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        discord_notify_scan_summary=True,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    summary_calls: list[dict[str, object]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            summary_calls.append(kwargs)
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            return True

        def diagnostics(self):
            return {}

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    response = trader.run_now()

    assert response["success"] is True
    assert len(summary_calls) == 1
    assert "cycle_id" in summary_calls[0]


def test_scan_summary_dedupes_identical_overlap(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        discord_notify_scan_summary=True,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    summary_calls: list[dict[str, object]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            summary_calls.append(kwargs)
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            return True

        def diagnostics(self):
            return {}

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    evaluations = [
        {
            "symbol": "AAPL",
            "action": "skipped",
            "decision_rule": "market_closed_extended_hours_disabled",
            "decision_reason": "market closed",
        }
    ]
    counts = {"submitted": 0, "rejected": 0, "skipped": 1, "hold": 0}

    trader._notify_scan_summary(
        cycle_id="cycle-1",
        all_symbols=["AAPL"],
        evaluations=evaluations,
        results=[],
        outcome_counts=counts,
    )
    trader._notify_scan_summary(
        cycle_id="cycle-2",
        all_symbols=["AAPL"],
        evaluations=evaluations,
        results=[],
        outcome_counts=counts,
    )

    assert len(summary_calls) == 1


def test_run_now_overlap_does_not_emit_duplicate_summary(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        discord_notify_scan_summary=True,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    summary_calls: list[dict[str, object]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            summary_calls.append(kwargs)
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            return True

        def diagnostics(self):
            return {}

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    trader._cycle_guard.acquire()
    try:
        response = trader.run_now()
    finally:
        trader._cycle_guard.release()

    assert response["success"] is True
    assert response["results"] == []
    assert summary_calls == []


def test_run_symbol_now_market_closed_returns_skipped(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        allow_extended_hours=False,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)

    def fake_session_status(_asset_class):
        return MarketSessionStatus(
            asset_class=AssetClass.EQUITY,
            is_open=False,
            session_state=SessionState.CLOSED,
            extended_hours=False,
            is_24_7=False,
        )

    monkeypatch.setattr(trader.market_data_service, "get_session_status", fake_session_status)
    result = trader.run_symbol_now("AAPL", AssetClass.EQUITY)

    assert result["action"] == "skipped"
    assert result["risk"]["rule"] == "market_closed_extended_hours_disabled"


def test_auto_trader_crypto_only_mode_evaluates_all_scanned_crypto_symbols(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        crypto_only_mode=True,
        crypto_symbols=["BTC/USD", "ETH/USD"],
        default_symbols=["AAPL", "BTC/USD"],
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    evaluated: list[str] = []
    scan_calls: list[dict[str, object]] = []
    snapshot_payload = NormalizedMarketSnapshot(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        last_trade_price=60000.0,
        evaluation_price=60000.0,
        quote_available=True,
        quote_stale=False,
        price_source_used="last_trade",
        session_state="always_open",
        exchange="CRYPTO",
        source="alpaca",
    ).to_dict()
    eth_snapshot_payload = {
        **snapshot_payload,
        "symbol": "ETH/USD",
        "last_trade_price": 3000.0,
        "evaluation_price": 3000.0,
    }

    monkeypatch.setattr(trader, "_sync_portfolio_from_broker", lambda: None)
    trader._last_signals = {"AAPL": {"symbol": "AAPL", "signal": "HOLD"}}
    monkeypatch.setattr(
        trader.scanner,
        "scan",
        lambda *args, **kwargs: (
            scan_calls.append({"args": args, "kwargs": kwargs}) or ScanResult(
                generated_at=datetime.now(timezone.utc),
                asset_class="crypto",
                scanned_count=2,
                opportunities=[],
                top_gainers=[],
                top_losers=[],
                unusual_volume=[],
                breakouts=[],
                pullbacks=[],
                volatility=[],
                momentum=[],
                regime_status={},
                errors=[],
                symbol_snapshots={"BTC/USD": snapshot_payload, "ETH/USD": eth_snapshot_payload},
            )
        ),
    )
    monkeypatch.setattr(
        trader,
        "_resolve_asset",
        lambda symbol, asset_class=None: AssetMetadata(
            symbol=symbol,
            name=symbol,
            asset_class=AssetClass.CRYPTO,
            exchange="CRYPTO",
            tradable=True,
        ),
    )

    def fake_evaluate(asset, prefer_primary_strategy=False, *, evaluation_mode="auto", precomputed_snapshot=None):
        evaluated.append(asset.symbol)
        return TradeSignal(
            symbol=asset.symbol,
            signal=Signal.HOLD,
            asset_class=AssetClass.CRYPTO,
            strategy_name="crypto_momentum_trend",
            reason="No signal",
            metrics={
                "decision_code": "no_signal",
                "normalized_snapshot": precomputed_snapshot or snapshot_payload,
                "price_source_used": "last_trade",
            },
        )

    monkeypatch.setattr(trader, "_evaluate_asset", fake_evaluate)

    response = trader.run_now()

    assert response["success"] is True
    assert evaluated == ["BTC/USD", "ETH/USD"]
    assert scan_calls[0]["kwargs"]["symbols"] == ["BTC/USD", "ETH/USD"]
    assert scan_calls[0]["kwargs"]["asset_class"] == AssetClass.CRYPTO
    assert trader.get_status()["last_scanned_symbols"] == ["BTC/USD", "ETH/USD"]
    assert set(trader.get_status()["last_signals"].keys()) == {"BTC/USD", "ETH/USD"}


def test_evaluate_asset_rebases_crypto_buy_levels_to_live_snapshot_price(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=True,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    asset = AssetMetadata(
        symbol="ETH/USD",
        name="Ethereum",
        asset_class=AssetClass.CRYPTO,
        exchange="CRYPTO",
        tradable=True,
    )
    snapshot = NormalizedMarketSnapshot(
        symbol="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        last_trade_price=80.0,
        bid_price=79.5,
        ask_price=80.5,
        quote_available=True,
        quote_stale=False,
        price_source_used="last_trade",
        evaluation_price=80.0,
        session_state="always_open",
        exchange="CRYPTO",
        source="alpaca",
    )

    class StubStrategy:
        name = "crypto_momentum_trend"

        @staticmethod
        def supports(_asset_class):
            return True

        @staticmethod
        def generate_signals(symbol, data, context=None):
            return [
                TradeSignal(
                    symbol=symbol,
                    signal=Signal.BUY,
                    asset_class=AssetClass.CRYPTO,
                    strategy_name="crypto_momentum_trend",
                    price=100.0,
                    entry_price=100.0,
                    stop_price=90.0,
                    target_price=122.0,
                    reason="Bullish setup",
                )
            ]

    bars = pd.DataFrame(
        {
            "Date": pd.date_range("2024-01-01", periods=5, tz="UTC"),
            "Open": [1, 2, 3, 4, 5],
            "High": [2, 3, 4, 5, 6],
            "Low": [0.5, 1.5, 2.5, 3.5, 4.5],
            "Close": [1, 2, 3, 4, 5],
            "Volume": [10, 11, 12, 13, 14],
        }
    )

    monkeypatch.setattr(trader, "_select_strategy_for_asset", lambda _asset: StubStrategy())
    monkeypatch.setattr(trader.market_data_service, "fetch_bars", lambda *args, **kwargs: bars)
    monkeypatch.setattr(
        trader.market_data_service,
        "get_session_status",
        lambda *args, **kwargs: MarketSessionStatus(
            asset_class=AssetClass.CRYPTO,
            is_open=True,
            session_state=SessionState.ALWAYS_OPEN,
            extended_hours=False,
            is_24_7=True,
        ),
    )

    signal = trader._evaluate_asset(
        asset,
        evaluation_mode="auto",
        precomputed_snapshot=snapshot.to_dict(),
    )

    assert signal.signal == Signal.BUY
    assert signal.entry_price == 80.0
    assert signal.stop_price == 70.0
    assert signal.target_price == 102.0

    decision = trader.risk_manager.evaluate_order(
        "ETH/USD",
        "BUY",
        1.0,
        signal.entry_price,
        stop_price=signal.stop_price,
        asset_class=AssetClass.CRYPTO,
    )

    assert decision.approved is True
    assert decision.rule == "approved"


def test_auto_trader_normalizes_buy_to_short_exit_when_short_position_exists() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        short_selling_enabled=True,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    trader.portfolio.positions["TSLA"] = Position(
        symbol="TSLA",
        quantity=5.0,
        entry_price=100.0,
        side="SHORT",
        current_price=90.0,
        asset_class=AssetClass.EQUITY,
    )
    asset = AssetMetadata(
        symbol="TSLA",
        name="Tesla",
        asset_class=AssetClass.EQUITY,
        exchange="NASDAQ",
        tradable=True,
    )
    signal = TradeSignal(
        symbol="TSLA",
        signal=Signal.BUY,
        asset_class=AssetClass.EQUITY,
        strategy_name="ema_crossover",
        price=90.0,
        entry_price=90.0,
        reason="Bullish crossover",
    )

    normalized = trader._normalize_signal_for_position_context(asset, signal)

    assert normalized.order_intent == "short_exit"
    assert normalized.reduce_only is True
    assert normalized.metrics["has_coverable_short_position"] is True
    assert normalized.metrics["position_direction"] == "short"


def test_evaluate_asset_uses_snapshot_exchange_metadata_and_normalizes_bad_timestamp(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    asset = AssetMetadata(
        symbol="BTC/USD",
        name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
        exchange="MOCK",
        tradable=True,
    )
    snapshot = NormalizedMarketSnapshot(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        last_trade_price=60250.0,
        bid_price=60200.0,
        ask_price=60300.0,
        quote_available=True,
        quote_stale=False,
        price_source_used="last_trade",
        evaluation_price=60250.0,
        session_state="always_open",
        exchange="CRYPTO",
        source="alpaca",
    )

    class StubStrategy:
        name = "crypto_momentum_trend"

        @staticmethod
        def supports(_asset_class):
            return True

        @staticmethod
        def generate_signals(symbol, data, context=None):
            return [
                TradeSignal(
                    symbol=symbol,
                    signal=Signal.HOLD,
                    asset_class=AssetClass.CRYPTO,
                    strategy_name="crypto_momentum_trend",
                    reason="No setup",
                    timestamp="59",
                )
            ]

    bars = pd.DataFrame(
        {
            "Date": pd.date_range("2024-01-01", periods=5, tz="UTC"),
            "Open": [1, 2, 3, 4, 5],
            "High": [2, 3, 4, 5, 6],
            "Low": [0.5, 1.5, 2.5, 3.5, 4.5],
            "Close": [1, 2, 3, 4, 5],
            "Volume": [10, 11, 12, 13, 14],
        }
    )

    monkeypatch.setattr(trader, "_select_strategy_for_asset", lambda _asset: StubStrategy())
    monkeypatch.setattr(trader.market_data_service, "fetch_bars", lambda *args, **kwargs: bars)
    monkeypatch.setattr(
        trader.market_data_service,
        "get_latest_quote",
        lambda *args, **kwargs: QuoteSnapshot(
            symbol="BTC/USD",
            asset_class=AssetClass.CRYPTO,
            bid_price=60200.0,
            ask_price=60300.0,
            timestamp=datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(
        trader.market_data_service,
        "get_session_status",
        lambda *args, **kwargs: MarketSessionStatus(
            asset_class=AssetClass.CRYPTO,
            is_open=True,
            session_state=SessionState.ALWAYS_OPEN,
            extended_hours=False,
            is_24_7=True,
        ),
    )

    signal = trader._evaluate_asset(
        asset,
        evaluation_mode="auto",
        precomputed_snapshot=snapshot.to_dict(),
    )

    assert signal.timestamp is None
    assert signal.metrics["exchange"] == "CRYPTO"
    assert signal.metrics["source"] == "alpaca"


def test_sync_portfolio_uses_crypto_session_context_in_crypto_only_mode(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        crypto_only_mode=True,
        crypto_symbols=["BTC/USD"],
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    requested_market_open: list[AssetClass] = []
    requested_session_status: list[AssetClass] = []
    account = trader.broker.get_account()

    monkeypatch.setattr(trader.broker, "get_positions", lambda: [])
    monkeypatch.setattr(trader.broker, "get_account", lambda: account)
    monkeypatch.setattr(trader.asset_catalog, "ensure_fresh", lambda: None)

    def fake_is_market_open(asset_class):
        requested_market_open.append(asset_class)
        return True

    def fake_session_status(asset_class):
        requested_session_status.append(asset_class)
        return MarketSessionStatus(
            asset_class=AssetClass.CRYPTO,
            is_open=True,
            session_state=SessionState.ALWAYS_OPEN,
            extended_hours=False,
            is_24_7=True,
        )

    monkeypatch.setattr(trader.broker, "is_market_open", fake_is_market_open)
    monkeypatch.setattr(trader.market_data_service, "get_session_status", fake_session_status)

    trader._sync_portfolio_from_broker()

    assert requested_market_open == [AssetClass.CRYPTO]
    assert requested_session_status == [AssetClass.CRYPTO]


def test_auto_trader_prefers_exit_signals_before_new_entries(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        universe_scan_enabled=False,
        default_symbols=["AAPL"],
        max_positions_total=2,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    trader.portfolio.positions["MSFT"] = Position(
        symbol="MSFT",
        quantity=5.0,
        entry_price=100.0,
        side="BUY",
        current_price=100.0,
        asset_class=AssetClass.EQUITY,
        initial_quantity=5.0,
        highest_price_since_entry=110.0,
        current_stop=95.0,
        entry_signal_metadata={
            "strategy_name": "equity_momentum_breakout",
            "stop_price": 95.0,
            "target_price": 120.0,
        },
    )
    processed_signals: list[tuple[str, str, str | None]] = []
    snapshots = {
        "AAPL": NormalizedMarketSnapshot(
            symbol="AAPL",
            asset_class=AssetClass.EQUITY,
            evaluation_price=150.0,
            quote_available=True,
            quote_stale=False,
            price_source_used="last_trade",
            session_state="regular",
            exchange="NASDAQ",
            source="mock",
        ).to_dict(),
        "MSFT": NormalizedMarketSnapshot(
            symbol="MSFT",
            asset_class=AssetClass.EQUITY,
            evaluation_price=94.0,
            quote_available=True,
            quote_stale=False,
            price_source_used="last_trade",
            session_state="regular",
            exchange="NASDAQ",
            source="mock",
        ).to_dict(),
    }

    monkeypatch.setattr(trader, "_sync_portfolio_from_broker", lambda: None)
    monkeypatch.setattr(
        trader.scanner,
        "scan",
        lambda *args, **kwargs: ScanResult(
            generated_at=datetime.now(timezone.utc),
            asset_class="equity",
            scanned_count=2,
            opportunities=[],
            top_gainers=[],
            top_losers=[],
            unusual_volume=[],
            breakouts=[],
            pullbacks=[],
            volatility=[],
            momentum=[],
            regime_status={},
            errors=[],
            symbol_snapshots=snapshots,
        ),
    )
    monkeypatch.setattr(
        trader,
        "_resolve_asset",
        lambda symbol, asset_class=None: AssetMetadata(
            symbol=symbol,
            name=symbol,
            asset_class=AssetClass.EQUITY,
            exchange="NASDAQ",
            tradable=True,
        ),
    )

    def fake_evaluate_exit(asset, *, normalized_snapshot, evaluation_mode):
        if asset.symbol != "MSFT":
            return None
        return TradeSignal(
            symbol="MSFT",
            signal=Signal.SELL,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            signal_type="exit",
            order_intent="long_exit",
            reduce_only=True,
            exit_stage="stop",
            position_size=5.0,
            price=94.0,
            entry_price=94.0,
            reason="Hard stop hit",
            metrics={"decision_code": "exit_signal", "normalized_snapshot": snapshots["MSFT"]},
        )

    monkeypatch.setattr(trader, "_evaluate_exit_signal", fake_evaluate_exit)
    monkeypatch.setattr(
        trader,
        "_evaluate_asset",
        lambda asset, **kwargs: TradeSignal(
            symbol=asset.symbol,
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=150.0,
            entry_price=150.0,
            stop_price=145.0,
            reason="Fresh breakout",
            metrics={"decision_code": "signal", "normalized_snapshot": snapshots[asset.symbol]},
        ),
    )
    monkeypatch.setattr(trader, "_enrich_signal", lambda signal, **kwargs: None)
    monkeypatch.setattr(trader, "_apply_ml_score_filter", lambda signal, **kwargs: signal)
    monkeypatch.setattr(trader, "_observe_broker_order_statuses", lambda cycle_id: None)

    def fake_process_signal(signal: TradeSignal):
        processed_signals.append((signal.symbol, signal.signal.value, signal.exit_stage))
        return {
            "latest_price": signal.price,
            "proposal": {"symbol": signal.symbol},
            "risk": {"rule": "approved", "reason": "ok", "details": {}},
            "action": "dry_run",
            "order": {"id": f"order-{signal.symbol}", "symbol": signal.symbol},
        }

    monkeypatch.setattr(trader.execution_service, "process_signal", fake_process_signal)

    response = trader.run_now()

    assert response["success"] is True
    assert processed_signals == [("MSFT", "SELL", "stop")]
    aapl_evaluation = next(item for item in trader.get_status()["last_symbol_evaluations"] if item["symbol"] == "AAPL")
    assert aapl_evaluation["action"] == "skipped"
