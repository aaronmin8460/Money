import pytest

from app.config.settings import Settings
from app.execution.execution_service import ExecutionService
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import PaperBroker, create_broker
from app.services.market_data import AlpacaMarketDataService
from app.strategies.base import Signal, TradeSignal


def test_alpaca_broker_requires_credentials() -> None:
    settings = Settings(broker_mode="alpaca", trading_enabled=False)
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        create_broker(settings)


def test_alpaca_market_data_requires_credentials() -> None:
    settings = Settings(broker_mode="alpaca", trading_enabled=False)
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        AlpacaMarketDataService(settings)


def test_paper_broker_status_safe_fallback() -> None:
    settings = Settings(broker_mode="paper", trading_enabled=False)
    broker = create_broker(settings)
    assert isinstance(broker, PaperBroker)
    assert broker.settings.broker_mode == "paper"


def test_run_once_dry_run_behavior() -> None:
    settings = Settings(broker_mode="paper", trading_enabled=False)
    broker = create_broker(settings)
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio)
    exec_service = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=True,
    )
    signal = TradeSignal(symbol="AAPL", signal=Signal.BUY, price=150.0)

    result = exec_service.process_signal(signal)

    assert result["action"] == "dry_run"
    assert result["risk"]["approved"] is True
    assert result["proposal"]["is_dry_run"] is True
