from __future__ import annotations

from unittest.mock import Mock, patch

import httpx

from app.config.settings import Settings
from app.domain.models import AssetClass
from app.execution.execution_service import ExecutionService
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import PaperBroker
from app.strategies.base import Signal, TradeSignal


def build_execution_service(**setting_overrides: object) -> tuple[ExecutionService, PaperBroker]:
    values = {
        "_env_file": None,
        "broker_mode": "paper",
        "trading_enabled": True,
        "discord_notifications_enabled": True,
        "discord_webhook_url": "https://discord.com/api/webhooks/test/token",
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


def build_response() -> Mock:
    response = Mock()
    response.raise_for_status.return_value = None
    return response


@patch("app.monitoring.discord_notifier.httpx.post")
def test_no_webhook_sent_when_notifications_disabled(mock_post: Mock) -> None:
    execution, _broker = build_execution_service(discord_notifications_enabled=False)

    result = execution.process_signal(build_signal())

    assert result["action"] == "submitted"
    mock_post.assert_not_called()


@patch("app.monitoring.discord_notifier.httpx.post")
def test_submitted_trade_sends_webhook(mock_post: Mock) -> None:
    mock_post.return_value = build_response()
    execution, _broker = build_execution_service()

    result = execution.process_signal(build_signal())

    assert result["action"] == "submitted"
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload["embeds"][0]["title"] == "Trade Submitted"
    assert any(field["name"] == "Symbol" and field["value"] == "AAPL" for field in payload["embeds"][0]["fields"])


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
    assert payload["embeds"][0]["title"] == "Dry Run Trade"


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
    assert payload["embeds"][0]["title"] == "Trade Rejected"


@patch("app.monitoring.discord_notifier.httpx.post")
def test_webhook_failures_do_not_break_trade_execution(mock_post: Mock) -> None:
    mock_post.side_effect = httpx.ConnectError("discord offline")
    execution, broker = build_execution_service()

    result = execution.process_signal(build_signal())

    assert result["action"] == "submitted"
    assert len(broker.list_orders()) == 1
