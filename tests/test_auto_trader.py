import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _build_broker_notifier(calls: list[dict[str, object]]):
    class StubNotifier:
        def send_system_notification(self, **kwargs):
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            calls.append(kwargs)
            return True

        def diagnostics(self):
            return {}

    return StubNotifier()


def _build_dust_exit_trader(monkeypatch) -> tuple[AutoTrader, list[str]]:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        auto_trade_enabled=True,
        crypto_only_mode=True,
        included_symbols=["ETH/USD"],
        crypto_symbols=["ETH/USD"],
        default_symbols=["ETH/USD"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    trader.portfolio.positions["ETH/USD"] = Position(
        symbol="ETH/USD",
        quantity=0.00000075,
        entry_price=1800.0,
        side="BUY",
        current_price=2000.0,
        asset_class=AssetClass.CRYPTO,
    )
    trader.tranche_state.upsert_plan(
        symbol="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        target_position_notional=500.0,
        tranche_weights=[1.0],
        scale_in_mode="confirmation",
        allow_average_down=False,
        decision_reason="Dust auto-trader test",
    )
    snapshot = NormalizedMarketSnapshot(
        symbol="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        last_trade_price=2000.0,
        mid_price=2000.0,
        evaluation_price=2000.0,
        quote_available=False,
        quote_stale=False,
        fallback_pricing_used=False,
        price_source_used="last_trade",
        session_state="always_open",
        exchange="CRYPTO",
        source="mock",
    ).to_dict()
    generated_exit_signals: list[str] = []

    monkeypatch.setattr(trader, "_sync_portfolio_from_broker", lambda: None)
    monkeypatch.setattr(
        trader.scanner,
        "scan",
        lambda *args, **kwargs: ScanResult(
            generated_at=datetime.now(timezone.utc),
            asset_class="crypto",
            scanned_count=1,
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
            symbol_snapshots={"ETH/USD": snapshot},
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
            fractionable=True,
        ),
    )

    def fake_evaluate_exit(asset, *, normalized_snapshot, evaluation_mode, regime_snapshot=None, news_features=None):
        if asset.symbol == "ETH/USD" and trader.portfolio.is_sellable_long_position(asset.symbol):
            generated_exit_signals.append(asset.symbol)
            return TradeSignal(
                symbol="ETH/USD",
                signal=Signal.SELL,
                asset_class=AssetClass.CRYPTO,
                strategy_name="crypto_momentum_trend",
                signal_type="exit",
                order_intent="long_exit",
                reduce_only=True,
                price=2000.0,
                entry_price=2000.0,
                reason="Dust exit",
                metrics={"decision_code": "exit_signal", "normalized_snapshot": snapshot},
            )
        return None

    monkeypatch.setattr(trader, "_evaluate_exit_signal", fake_evaluate_exit)
    monkeypatch.setattr(
        trader,
        "_evaluate_asset",
        lambda asset, **kwargs: TradeSignal(
            symbol=asset.symbol,
            signal=Signal.HOLD,
            asset_class=asset.asset_class,
            strategy_name="crypto_momentum_trend",
            price=2000.0,
            reason="No position",
            metrics={"decision_code": "no_signal", "normalized_snapshot": snapshot},
        ),
    )
    monkeypatch.setattr(trader, "_enrich_signal", lambda signal, **kwargs: None)
    monkeypatch.setattr(trader, "_apply_ml_score_filter", lambda signal, **kwargs: signal)
    monkeypatch.setattr(trader, "_observe_broker_order_statuses", lambda cycle_id: None)
    return trader, generated_exit_signals


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


def test_auto_trader_status_surfaces_dust_resolution(monkeypatch) -> None:
    trader, generated_exit_signals = _build_dust_exit_trader(monkeypatch)

    response = trader.run_now()
    status = trader.get_status()

    assert response["success"] is True
    assert generated_exit_signals == ["ETH/USD"]
    assert response["results"][0]["action"] == "dust_resolved"
    assert status["open_positions_count"] == 0
    assert status["latest_dust_resolution"]["action"] == "dust_resolved"
    assert status["last_run_result"]["action_counts"]["dust_resolved"] == 1
    assert status["last_symbol_evaluations"][0]["classification"] == "dust_resolved"
    tranche = next(item for item in status["tranche_state"] if item["symbol"] == "ETH/USD")
    assert tranche["plan_status"] == "closed"


def test_auto_trader_does_not_retry_exit_after_dust_resolution(monkeypatch) -> None:
    trader, generated_exit_signals = _build_dust_exit_trader(monkeypatch)

    first = trader.run_now()
    second = trader.run_now()

    assert first["success"] is True
    assert second["success"] is True
    assert generated_exit_signals == ["ETH/USD"]
    assert first["results"][0]["action"] == "dust_resolved"
    assert second["results"] == []
    assert trader.get_status()["open_positions_count"] == 0


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

    def fake_evaluate_exit(asset, *, normalized_snapshot, evaluation_mode, regime_snapshot=None, news_features=None):
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


def test_broker_order_status_cache_persists_and_reloads_correctly(tmp_path: Path) -> None:
    cache_path = tmp_path / "broker_order_status_memory.json"
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        log_dir=str(tmp_path),
        broker_order_status_cache_path=str(cache_path),
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    trader._order_status_memory = {
        "broker-1": {
            "status": "filled",
            "last_seen_at": "2026-04-11T12:00:00Z",
            "symbol": "AAPL",
            "side": "buy",
        }
    }

    trader._persist_broker_order_status_memory()
    reloaded = AutoTrader(settings)

    assert reloaded._order_status_memory["broker-1"]["status"] == "filled"
    assert reloaded._order_status_memory["broker-1"]["symbol"] == "AAPL"


def test_restarting_with_existing_filled_order_does_not_resend_alert(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "broker_order_status_memory.json"
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        log_dir=str(tmp_path),
        broker_order_status_cache_path=str(cache_path),
        broker_order_status_suppress_startup_replay=True,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        auto_trader_lock_path=_test_lock_path(),
    )
    order = {
        "id": "broker-1",
        "symbol": "AAPL",
        "side": "buy",
        "status": "filled",
        "filled_at": "2026-04-11T11:00:00Z",
    }
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda _settings: _build_broker_notifier(calls))

    first_trader = AutoTrader(settings)
    monkeypatch.setattr(first_trader.broker, "list_orders", lambda: [order])
    first_trader._observe_broker_order_statuses(cycle_id="cycle-1")

    restarted_trader = AutoTrader(settings)
    monkeypatch.setattr(restarted_trader.broker, "list_orders", lambda: [order])
    restarted_trader._observe_broker_order_statuses(cycle_id="cycle-2")

    assert calls == []


def test_real_status_transition_after_startup_sends_alert(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "broker_order_status_memory.json"
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        log_dir=str(tmp_path),
        broker_order_status_cache_path=str(cache_path),
        broker_order_status_suppress_startup_replay=True,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        auto_trader_lock_path=_test_lock_path(),
    )
    states = [
        [
            {
                "id": "broker-2",
                "symbol": "MSFT",
                "side": "buy",
                "status": "accepted",
                "submitted_at": "2026-04-11T11:00:00Z",
            }
        ],
        [
            {
                "id": "broker-2",
                "symbol": "MSFT",
                "side": "buy",
                "status": "filled",
                "filled_at": "2026-04-11T11:05:00Z",
            }
        ],
    ]
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda _settings: _build_broker_notifier(calls))

    trader = AutoTrader(settings)
    monkeypatch.setattr(trader.broker, "list_orders", lambda: states.pop(0))
    trader._observe_broker_order_statuses(cycle_id="cycle-1")
    trader._observe_broker_order_statuses(cycle_id="cycle-2")

    assert [call["status"] for call in calls] == ["filled"]


def test_newly_submitted_order_after_startup_sends_notification_on_first_poll(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "broker_order_status_memory.json"
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        log_dir=str(tmp_path),
        broker_order_status_cache_path=str(cache_path),
        broker_order_status_suppress_startup_replay=True,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        auto_trader_lock_path=_test_lock_path(),
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda _settings: _build_broker_notifier(calls))

    trader = AutoTrader(settings)
    trader._record_order({"id": "broker-3", "symbol": "NVDA"})
    monkeypatch.setattr(
        trader.broker,
        "list_orders",
        lambda: [
            {
                "id": "broker-3",
                "symbol": "NVDA",
                "side": "buy",
                "status": "accepted",
                "submitted_at": "2026-04-11T11:15:00Z",
            }
        ],
    )

    trader._observe_broker_order_statuses(cycle_id="cycle-1")

    assert [call["status"] for call in calls] == ["accepted"]


def test_corrupt_broker_status_cache_fails_safely(tmp_path: Path) -> None:
    cache_path = tmp_path / "broker_order_status_memory.json"
    cache_path.write_text("{not-json", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        log_dir=str(tmp_path),
        broker_order_status_cache_path=str(cache_path),
        auto_trader_lock_path=_test_lock_path(),
    )

    trader = AutoTrader(settings)

    assert trader._order_status_memory == {}


def test_buy_ranking_and_exposure_gating_behavior() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        max_position_notional=5_000.0,
        max_symbol_allocation_pct=0.05,
        max_asset_class_allocation_pct={"equity": 0.05, "etf": 0.05, "crypto": 0.10, "option": 0.02},
        max_concurrent_positions=2,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    trader.portfolio.positions["AAPL"] = Position(
        symbol="AAPL",
        quantity=50.0,
        entry_price=100.0,
        side="BUY",
        current_price=100.0,
        asset_class=AssetClass.EQUITY,
    )
    equity_candidate = TradeSignal(
        symbol="MSFT",
        signal=Signal.BUY,
        asset_class=AssetClass.EQUITY,
        strategy_name="equity_momentum_breakout",
        price=100.0,
        entry_price=100.0,
        stop_price=95.0,
        liquidity_score=0.9,
        confidence_score=0.8,
        metrics={
            "strategy_score": 0.8,
            "reward_risk_ratio": 2.0,
            "breakout_distance_atr": 0.2,
            "ml": {"score": 0.9},
        },
    )
    crypto_candidate = TradeSignal(
        symbol="BTC/USD",
        signal=Signal.BUY,
        asset_class=AssetClass.CRYPTO,
        strategy_name="crypto_momentum_trend",
        price=50_000.0,
        entry_price=50_000.0,
        stop_price=48_000.0,
        liquidity_score=0.5,
        confidence_score=0.6,
        metrics={
            "strategy_score": 0.5,
            "reward_risk_ratio": 1.4,
            "breakout_distance_atr": 0.3,
            "ml": {"score": 0.55},
        },
    )

    selected = trader._select_ranked_buy_signals([equity_candidate, crypto_candidate])

    assert [signal.symbol for signal in selected] == ["BTC/USD"]
    assert equity_candidate.metrics["buy_ranking"]["selection_rule"] == "portfolio_exposure_limit"
    assert crypto_candidate.metrics["buy_ranking"]["selection_rule"] == "selected_by_rank"


def test_evaluate_asset_uses_separate_entry_and_regime_timeframes(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        entry_timeframe_by_asset_class={"crypto": "15Min"},
        regime_timeframe_by_asset_class={"crypto": "4H"},
        lookback_bars_by_asset_class={"crypto": 80},
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    asset = AssetMetadata(
        symbol="ETH/USD",
        name="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        exchange="CRYPTO",
        tradable=True,
        fractionable=True,
    )
    snapshot = NormalizedMarketSnapshot(
        symbol="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        last_trade_price=3000.0,
        evaluation_price=3000.0,
        quote_available=True,
        quote_stale=False,
        price_source_used="last_trade",
        session_state="always_open",
        exchange="CRYPTO",
        source="mock",
    ).to_dict()
    fetch_calls: list[tuple[str, str, int]] = []

    class RecordingStrategy:
        name = "crypto_momentum_trend"

        def __init__(self) -> None:
            self.seen_data = None
            self.seen_context = None

        def supports(self, asset_class: AssetClass) -> bool:
            return asset_class == AssetClass.CRYPTO

        def generate_signals(self, symbol: str, data, context=None):
            self.seen_data = data
            self.seen_context = context
            return [
                TradeSignal(
                    symbol=symbol,
                    signal=Signal.BUY,
                    asset_class=AssetClass.CRYPTO,
                    strategy_name=self.name,
                    price=3000.0,
                    entry_price=3000.0,
                    stop_price=2940.0,
                    target_price=3120.0,
                    reason="Momentum aligned",
                    metrics={"decision_code": "signal", "strategy_score": 0.7},
                )
            ]

    strategy = RecordingStrategy()

    def fake_fetch_bars(symbol: str, timeframe: str | None = None, limit: int = 50, asset_class=None):
        fetch_calls.append((symbol, str(timeframe), int(limit)))
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        if timeframe == "4H":
            closes = [200.0, 201.0, 202.0, 203.0, 204.0, 205.0]
        return pd.DataFrame(
            {
                "Open": closes,
                "High": [value + 1 for value in closes],
                "Low": [value - 1 for value in closes],
                "Close": closes,
                "Volume": [1000, 1100, 1200, 1300, 1400, 1500],
            }
        )

    monkeypatch.setattr(trader, "_select_strategy_for_asset", lambda _asset: strategy)
    monkeypatch.setattr(trader.market_data_service, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        trader.market_data_service,
        "get_latest_quote",
        lambda symbol, asset_class: QuoteSnapshot(symbol=symbol, asset_class=AssetClass.CRYPTO),
    )

    result = trader._evaluate_asset(asset, precomputed_snapshot=snapshot)

    assert ("ETH/USD", "15Min", 80) in fetch_calls
    assert ("ETH/USD", "4H", 80) in fetch_calls
    assert isinstance(strategy.seen_data, dict)
    assert float(strategy.seen_data["entry"].iloc[-1]["Close"]) == 105.0
    assert float(strategy.seen_data["regime"].iloc[-1]["Close"]) == 205.0
    assert result.metrics["entry_timeframe"] == "15Min"
    assert result.metrics["regime_timeframe"] == "4H"


def test_scan_and_trade_honors_per_asset_class_cadence(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        enabled_asset_classes=["equity", "crypto"],
        etf_trading_enabled=False,
        scan_interval_seconds_by_asset_class={"equity": 300, "crypto": 60},
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    now = datetime.utcnow()
    trader._last_asset_class_run_at = {
        "equity": now - timedelta(seconds=30),
        "crypto": now - timedelta(seconds=120),
    }
    scan_calls: list[str] = []

    monkeypatch.setattr(trader, "_sync_portfolio_from_broker", lambda: None)
    monkeypatch.setattr(trader, "_observe_broker_order_statuses", lambda cycle_id: None)

    def fake_scan(*, asset_class=None, **kwargs):
        scan_calls.append(asset_class.value)
        return ScanResult(
            generated_at=datetime.now(timezone.utc),
            asset_class=asset_class.value,
            scanned_count=0,
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
            symbol_snapshots={},
        )

    monkeypatch.setattr(trader.scanner, "scan", fake_scan)

    response = trader._scan_and_trade(mode="background_loop", respect_cadence=True)

    assert response == []
    assert scan_calls == ["crypto"]
    assert trader.get_status()["last_scan_overview"]["due_asset_classes"] == ["crypto"]
    assert "equity" in trader.get_status()["last_cadence_diagnostics"]["skipped_asset_classes"]


def test_open_positions_are_monitored_even_without_new_entries(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        enabled_asset_classes=["crypto"],
        equity_trading_enabled=False,
        etf_trading_enabled=False,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    trader.portfolio.positions["ETH/USD"] = Position(
        symbol="ETH/USD",
        quantity=0.25,
        entry_price=2000.0,
        side="BUY",
        current_price=2100.0,
        asset_class=AssetClass.CRYPTO,
    )
    snapshot = NormalizedMarketSnapshot(
        symbol="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        last_trade_price=2100.0,
        evaluation_price=2100.0,
        quote_available=True,
        quote_stale=False,
        price_source_used="last_trade",
        session_state="always_open",
        exchange="CRYPTO",
        source="mock",
    ).to_dict()
    scan_kwargs: list[dict[str, object]] = []
    evaluated_symbols: list[str] = []

    monkeypatch.setattr(trader, "_sync_portfolio_from_broker", lambda: None)
    monkeypatch.setattr(trader, "_observe_broker_order_statuses", lambda cycle_id: None)
    monkeypatch.setattr(trader, "_evaluate_exit_signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(trader, "_enrich_signal", lambda signal, **kwargs: None)
    monkeypatch.setattr(trader, "_apply_ml_score_filter", lambda signal, **kwargs: signal)
    monkeypatch.setattr(
        trader.execution_service,
        "process_signal",
        lambda signal: {
            "latest_price": signal.price,
            "proposal": {"symbol": signal.symbol},
            "risk": {"rule": "hold", "reason": signal.reason, "details": {}},
            "action": "skipped",
            "order": None,
        },
    )

    def fake_scan(*args, **kwargs):
        scan_kwargs.append(kwargs)
        return ScanResult(
            generated_at=datetime.now(timezone.utc),
            asset_class="crypto",
            scanned_count=1,
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
            symbol_snapshots={"ETH/USD": snapshot},
            symbol_inclusion_reasons={"ETH/USD": ["open_position"]},
            timeframes_by_asset_class={
                "crypto": {
                    "scanner_timeframe": "15Min",
                    "entry_timeframe": "15Min",
                    "regime_timeframe": "4H",
                    "lookback_bars": 160,
                }
            },
        )

    def fake_evaluate_asset(asset, **kwargs):
        evaluated_symbols.append(asset.symbol)
        return TradeSignal(
            symbol=asset.symbol,
            signal=Signal.HOLD,
            asset_class=asset.asset_class,
            strategy_name="crypto_momentum_trend",
            price=2100.0,
            entry_price=2100.0,
            reason="Monitor open position",
            metrics={"decision_code": "no_signal", "normalized_snapshot": snapshot},
        )

    monkeypatch.setattr(trader.scanner, "scan", fake_scan)
    monkeypatch.setattr(trader, "_evaluate_asset", fake_evaluate_asset)

    response = trader._scan_and_trade(mode="background_loop", respect_cadence=True)

    assert response
    assert evaluated_symbols == ["ETH/USD"]
    assert scan_kwargs[0]["required_symbols"] == ["ETH/USD"]


def test_ranked_buy_selection_stays_diverse_without_duplicate_spam() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        max_position_notional=10_000.0,
        max_symbol_allocation_pct=0.10,
        max_asset_class_allocation_pct={"equity": 0.20, "etf": 0.20, "crypto": 0.20, "option": 0.02},
        max_concurrent_positions=3,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    signals = [
        TradeSignal(
            symbol="MSFT",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            entry_price=100.0,
            stop_price=95.0,
            liquidity_score=0.9,
            confidence_score=0.8,
            metrics={"strategy_score": 0.8, "reward_risk_ratio": 2.0, "breakout_distance_atr": 0.2, "ml": {"score": 0.9}},
        ),
        TradeSignal(
            symbol="SPY",
            signal=Signal.BUY,
            asset_class=AssetClass.ETF,
            strategy_name="equity_momentum_breakout",
            price=500.0,
            entry_price=500.0,
            stop_price=490.0,
            liquidity_score=0.8,
            confidence_score=0.75,
            metrics={"strategy_score": 0.75, "reward_risk_ratio": 1.8, "breakout_distance_atr": 0.25, "ml": {"score": 0.8}},
        ),
        TradeSignal(
            symbol="BTC/USD",
            signal=Signal.BUY,
            asset_class=AssetClass.CRYPTO,
            strategy_name="crypto_momentum_trend",
            price=50_000.0,
            entry_price=50_000.0,
            stop_price=48_500.0,
            liquidity_score=0.7,
            confidence_score=0.7,
            metrics={"strategy_score": 0.7, "reward_risk_ratio": 1.7, "breakout_distance_atr": 0.3, "ml": {"score": 0.7}},
        ),
        TradeSignal(
            symbol="MSFT",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            entry_price=100.0,
            stop_price=96.0,
            liquidity_score=0.6,
            confidence_score=0.55,
            metrics={"strategy_score": 0.5, "reward_risk_ratio": 1.2, "breakout_distance_atr": 0.35, "ml": {"score": 0.4}},
        ),
    ]

    selected = trader._select_ranked_buy_signals(signals)

    assert [signal.symbol for signal in selected] == ["MSFT", "SPY", "BTC/USD"]
    assert len({signal.symbol for signal in selected}) == 3
    assert signals[-1].metrics["buy_ranking"]["selection_rule"] == "duplicate_same_cycle"
