import tempfile
import uuid
from datetime import datetime, timezone

import pandas as pd

from app.services import auto_trader as auto_trader_module
from app.config.settings import Settings
from app.domain.models import AssetClass, AssetMetadata, MarketSessionStatus, NormalizedMarketSnapshot, QuoteSnapshot, SessionState
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
        lambda *args, **kwargs: ScanResult(
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
    assert trader.get_status()["last_scanned_symbols"] == ["BTC/USD", "ETH/USD"]
    assert set(trader.get_status()["last_signals"].keys()) == {"BTC/USD", "ETH/USD"}


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
