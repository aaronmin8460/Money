from __future__ import annotations

from unittest.mock import Mock, patch

import httpx

from app.config.settings import Settings
from app.domain.models import AssetClass
from app.execution.execution_service import ExecutionService
from app.monitoring.discord_notifier import (
    DiscordNotifier,
    build_system_notification_payload,
    build_trade_notification_payload,
)
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskDecision, RiskManager
from app.services.broker import OrderRequest, PaperBroker
from app.strategies.base import Signal, TradeSignal


def build_execution_service(**setting_overrides: object) -> tuple[ExecutionService, PaperBroker]:
    values = {
        "_env_file": None,
        "broker_mode": "paper",
        "trading_enabled": True,
        "discord_notifications_enabled": True,
        "discord_webhook_url": "https://discord.com/api/webhooks/test-id/test-token",
        "min_avg_volume": 1,
        "min_dollar_volume": 1,
        "min_price": 1,
    }
    values.update(setting_overrides)
    settings = Settings(**values)
    broker = PaperBroker(settings=settings)
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    execution = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=not settings.trading_enabled,
        settings=settings,
    )
    return execution, broker


def build_signal() -> TradeSignal:
    return TradeSignal(
        symbol="AAPL",
        signal=Signal.BUY,
        asset_class=AssetClass.EQUITY,
        strategy_name="test_strategy",
        price=150.0,
        stop_price=145.0,
        reason="test notification",
    )


def build_response(status_code: int = 204, text: str = "") -> Mock:
    response = Mock()
    response.status_code = status_code
    response.text = text
    return response


def test_build_system_notification_payload_start_stop_is_compact() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=True,
        auto_trade_enabled=True,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
    )

    payload = build_system_notification_payload(
        settings=settings,
        event="Bot started",
        reason="background loop started",
        category="start_stop",
        timestamp="2026-04-10T13:55:57Z",
    )
    embed = payload["embeds"][0]

    assert "content" not in payload
    assert embed["title"] == "🔵 PAPER | Bot started"
    assert embed["description"] == (
        "Mode: PAPER\n"
        "Auto-trade: enabled\n"
        "Time: 2026-04-10 13:55:57 UTC"
    )


def test_build_trade_notification_payload_is_compact() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=True,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
    )
    signal = TradeSignal(
        symbol="BTC/USD",
        signal=Signal.BUY,
        asset_class=AssetClass.CRYPTO,
        strategy_name="regime_momentum_breakout",
        price=65000.0,
        reason="Momentum breakout",
    )
    proposal = OrderRequest(
        symbol="BTC/USD",
        side=Signal.BUY.value,
        quantity=0.001,
        asset_class=AssetClass.CRYPTO,
        price=65000.0,
        is_dry_run=False,
    )
    order = {
        "id": "order-123",
        "status": "FILLED",
        "quantity": 0.001,
        "price": 65000.0,
        "executed_at": "2026-04-10T14:05:00Z",
        "is_dry_run": False,
    }

    payload = build_trade_notification_payload(
        settings=settings,
        action="submitted",
        signal=signal,
        proposal=proposal,
        risk=RiskDecision(True, "Order approved by risk manager.", rule="approved"),
        order=order,
    )
    embed = payload["embeds"][0]

    assert "content" not in payload
    assert embed["title"] == "🟢 PAPER | BUY BTC/USD submitted"
    assert embed["description"] == (
        "Momentum breakout\n\n"
        "Qty: 0.001\n"
        "Price: $65,000.00\n"
        "Strategy: regime_momentum_breakout\n"
        "Status: FILLED\n"
        "Order ID: order-123\n"
        "Time: 2026-04-10 14:05:00 UTC"
    )


@patch("app.monitoring.discord_notifier.httpx.post")
def test_no_webhook_sent_when_notifications_disabled(mock_post: Mock) -> None:
    execution, _broker = build_execution_service(discord_notifications_enabled=False)

    result = execution.process_signal(build_signal())

    assert result["action"] == "submitted"
    mock_post.assert_not_called()


@patch("app.monitoring.discord_notifier.httpx.post")
def test_submitted_trade_sends_embed_webhook_without_duplicate_content(mock_post: Mock) -> None:
    mock_post.return_value = build_response()
    execution, _broker = build_execution_service()

    result = execution.process_signal(build_signal())

    assert result["action"] == "submitted"
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    embed = payload["embeds"][0]
    assert payload.get("content") is None
    assert embed["title"] == "🟢 PAPER | BUY AAPL submitted"
    assert embed["description"].startswith("test notification\n\nQty: ")
    assert "Price: $150.00" in embed["description"]
    assert "Strategy: test_strategy" in embed["description"]


@patch("app.monitoring.discord_notifier.httpx.post")
def test_dry_runs_send_only_when_enabled(mock_post: Mock) -> None:
    mock_post.return_value = build_response()
    disabled_execution, _broker = build_execution_service(
        trading_enabled=False,
        discord_notify_dry_runs=False,
    )

    disabled_result = disabled_execution.process_signal(build_signal())

    assert disabled_result["action"] == "dry_run"
    mock_post.assert_not_called()

    enabled_execution, _broker = build_execution_service(
        trading_enabled=False,
        discord_notify_dry_runs=True,
    )

    enabled_result = enabled_execution.process_signal(build_signal())

    assert enabled_result["action"] == "dry_run"
    assert mock_post.call_count == 1
    payload = mock_post.call_args.kwargs["json"]
    embed = payload["embeds"][0]
    assert payload.get("content") is None
    assert embed["title"] == "🟡 PAPER | BUY AAPL dry run"
    assert "test notification" in embed["description"]
    assert "Price: $150.00" in embed["description"]


@patch("app.monitoring.discord_notifier.httpx.post")
def test_rejection_notifications_respect_settings(mock_post: Mock) -> None:
    mock_post.return_value = build_response()
    disabled_execution, _broker = build_execution_service(
        max_positions_total=0,
        discord_notify_rejections=False,
    )

    disabled_result = disabled_execution.process_signal(build_signal())

    assert disabled_result["action"] == "rejected"
    mock_post.assert_not_called()

    enabled_execution, _broker = build_execution_service(
        max_positions_total=0,
        discord_notify_rejections=True,
    )

    enabled_result = enabled_execution.process_signal(build_signal())

    assert enabled_result["action"] == "rejected"
    assert mock_post.call_count == 1
    payload = mock_post.call_args.kwargs["json"]
    embed = payload["embeds"][0]
    assert payload.get("content") is None
    assert embed["title"] == "🟠 PAPER | BUY AAPL rejected"
    assert embed["description"].startswith("Maximum simultaneous positions (0) reached.")
    assert "Rule: Position Count" in embed["description"]
    assert "Risk Reason:" not in embed["description"]
    assert "Asset Class:" not in embed["description"]


@patch("app.monitoring.discord_notifier.httpx.post")
def test_non_2xx_webhook_logs_sanitized_target(mock_post: Mock, caplog) -> None:
    mock_post.return_value = build_response(status_code=400, text="bad webhook request")
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/1234567890/super-secret-token",
    )
    notifier = DiscordNotifier(settings)

    sent = notifier.send_system_notification(
        event="Bot started",
        reason="background loop started",
        category="start_stop",
    )

    assert sent is False
    assert "status 400" in caplog.text
    assert "bad webhook request" in caplog.text
    assert "discord.com/api/webhooks/1234...7890/***" in caplog.text
    assert "super-secret-token" not in caplog.text


@patch("app.monitoring.discord_notifier.httpx.post")
def test_webhook_failures_do_not_break_trade_execution(mock_post: Mock) -> None:
    mock_post.side_effect = httpx.ConnectError("discord offline")
    execution, broker = build_execution_service()

    result = execution.process_signal(build_signal())

    assert result["action"] == "submitted"
    assert len(broker.list_orders()) == 1
