import pytest
from unittest.mock import Mock, patch

from app.config.settings import Settings
from app.execution.execution_service import ExecutionService
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import AlpacaBroker, BrokerAuthError, BrokerConnectionError, BrokerUpstreamError, PaperBroker, create_broker
from app.services.market_data import AlpacaMarketDataService
from app.strategies.base import Signal, TradeSignal


def test_alpaca_broker_requires_credentials() -> None:
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        settings = Settings(broker_mode="alpaca", alpaca_api_key=None, alpaca_secret_key=None, trading_enabled=False)
        create_broker(settings)


def test_alpaca_market_data_requires_credentials() -> None:
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        settings = Settings(broker_mode="alpaca", alpaca_api_key=None, alpaca_secret_key=None, trading_enabled=False)
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


@patch('httpx.Client.request')
def test_alpaca_broker_get_account_success(mock_request) -> None:
    mock_account_response = Mock()
    mock_account_response.raise_for_status.return_value = None
    mock_account_response.json.return_value = {
        "cash": "50000.0",
        "equity": "100000.0",
        "buying_power": "150000.0"
    }
    mock_positions_response = Mock()
    mock_positions_response.raise_for_status.return_value = None
    mock_positions_response.json.return_value = [{"symbol": "AAPL"}, {"symbol": "SPY"}]
    
    def mock_request_side_effect(*args, **kwargs):
        if '/v2/account' in args[1]:
            return mock_account_response
        elif '/v2/positions' in args[1]:
            return mock_positions_response
        raise ValueError("Unexpected URL")
    
    mock_request.side_effect = mock_request_side_effect
    
    settings = Settings(
        broker_mode="alpaca",
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        alpaca_base_url="https://paper-api.alpaca.markets"
    )
    broker = AlpacaBroker(settings)
    account = broker.get_account()
    
    assert account.cash == 50000.0
    assert account.equity == 100000.0
    assert account.positions == 2
    assert account.buying_power == 150000.0


@patch('httpx.Client.request')
def test_alpaca_broker_get_account_positions_fail_gracefully(mock_request) -> None:
    mock_account_response = Mock()
    mock_account_response.raise_for_status.return_value = None
    mock_account_response.json.return_value = {
        "cash": "50000.0",
        "equity": "100000.0",
        "buying_power": "150000.0"
    }
    
    def mock_request_side_effect(*args, **kwargs):
        if '/v2/account' in args[1]:
            return mock_account_response
        elif '/v2/positions' in args[1]:
            raise BrokerAuthError("Auth failed")
        raise ValueError("Unexpected URL")
    
    mock_request.side_effect = mock_request_side_effect
    
    settings = Settings(
        broker_mode="alpaca",
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        alpaca_base_url="https://paper-api.alpaca.markets"
    )
    broker = AlpacaBroker(settings)
    account = broker.get_account()
    
    assert account.cash == 50000.0
    assert account.equity == 100000.0
    assert account.positions == 0  # Degraded gracefully
    assert account.buying_power == 150000.0


@patch('httpx.Client.request')
def test_alpaca_broker_auth_error(mock_request) -> None:
    mock_request.side_effect = BrokerAuthError("Auth failed")
    
    settings = Settings(
        broker_mode="alpaca",
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        alpaca_base_url="https://paper-api.alpaca.markets"
    )
    broker = AlpacaBroker(settings)
    
    with pytest.raises(BrokerAuthError, match="Auth failed"):
        broker.get_account()
