from __future__ import annotations

from unittest.mock import Mock, patch

import httpx

from app.config.settings import Settings
from app.domain.models import AssetClass
from app.execution.execution_service import ExecutionService
from app.monitoring.discord_notifier import (
    DiscordNotifier,
    build_system_notification_message,
    build_trade_notification_message,
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


def test_build_system_notification_message_plain_text() -> None:
    message = build_system_notification_message(
        mode_label="PAPER",
        event="Bot started",
        reason="background loop started",
        timestamp="2026-04-10T13:55:57Z",
    )

    assert message == (
        "[Money Bot][PAPER]\n"
        "Bot started\n"
        "Reason: background loop started\n"
        "Time: 2026-04-10T13:55:57Z"
    )


def test_build_trade_notification_message_plain_text() -> None:
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

    message = build_trade_notification_message(
        settings=settings,
        action="submitted",
        signal=signal,
        proposal=proposal,
        risk=RiskDecision(True, "Order approved by risk manager.", rule="approved"),
        order=order,
    )

    assert message == (
        "[Money Bot][PAPER]\n"
        "Trade executed\n"
        "Symbol: BTC/USD\n"
        "Asset Class: crypto\n"
        "Side: BUY\n"
        "Quantity: 0.001\n"
        "Price: 65000\n"
        "Strategy: regime_momentum_breakout\n"
        "Action: SUBMITTED\n"
        "Order Status: FILLED\n"
        "Order ID: order-123\n"
        "Time: 2026-04-10T14:05:00Z"
    )


@patch("app.monitoring.discord_notifier.httpx.post")
def test_no_webhook_sent_when_notifications_disabled(mock_post: Mock) -> None:
    execution, _broker = build_execution_service(discord_notifications_enabled=False)

    result = execution.process_signal(build_signal())

    assert result["action"] == "submitted"
    mock_post.assert_not_called()


@patch("app.monitoring.discord_notifier.httpx.post")
def test_submitted_trade_sends_plain_text_webhook(mock_post: Mock) -> None:
    mock_post.return_value = build_response()
    execution, _broker = build_execution_service()

    result = execution.process_signal(build_signal())

    assert result["action"] == "submitted"
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert "embeds" not in payload
    assert payload["content"].startswith("[Money Bot][PAPER]\nTrade executed\n")
    assert "Symbol: AAPL" in payload["content"]


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
    assert "[Money Bot][DRY_RUN]" in payload["content"]
    assert "Dry run trade" in payload["content"]


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
    assert "Trade rejected" in payload["content"]
    assert "Risk Reason:" in payload["content"]


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
