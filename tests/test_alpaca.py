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
        settings = Settings(_env_file=None, broker_mode="paper", alpaca_api_key=None, alpaca_secret_key=None, trading_enabled=False)
        create_broker(settings)


def test_alpaca_market_data_requires_credentials() -> None:
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        settings = Settings(_env_file=None, broker_mode="paper", alpaca_api_key=None, alpaca_secret_key=None, trading_enabled=False)
        AlpacaMarketDataService(settings)


def test_mock_broker_status_safe_fallback() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False)
    broker = create_broker(settings)
    assert isinstance(broker, PaperBroker)
    assert broker.settings.broker_mode == "mock"


def test_run_once_dry_run_behavior() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False)
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
        _env_file=None,
        broker_mode="paper",
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
        _env_file=None,
        broker_mode="paper",
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
        _env_file=None,
        broker_mode="paper",
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        alpaca_base_url="https://paper-api.alpaca.markets"
    )
    broker = AlpacaBroker(settings)
    
    with pytest.raises(BrokerAuthError, match="Auth failed"):
        broker.get_account()


@patch("httpx.Client.request")
def test_paper_execution_path_only_submits_remote_order_when_trading_enabled(mock_request) -> None:
    mock_account_response = Mock()
    mock_account_response.raise_for_status.return_value = None
    mock_account_response.json.return_value = {
        "cash": "100000.0",
        "equity": "100000.0",
        "buying_power": "100000.0",
    }
    mock_order_response = Mock()
    mock_order_response.raise_for_status.return_value = None
    mock_order_response.json.return_value = {
        "id": "order-1",
        "symbol": "AAPL",
        "status": "accepted",
        "qty": "10",
    }

    def side_effect(method, path, *args, **kwargs):
        if path == "/v2/account":
            return mock_account_response
        if path == "/v2/clock":
            clock_response = Mock()
            clock_response.raise_for_status.return_value = None
            clock_response.json.return_value = {"is_open": True}
            return clock_response
        if path == "/v2/assets/AAPL":
            asset_response = Mock()
            asset_response.raise_for_status.return_value = None
            asset_response.json.return_value = {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "asset_class": "us_equity",
                "tradable": True,
                "fractionable": False,
                "shortable": True,
                "easy_to_borrow": True,
                "marginable": True,
                "exchange": "NASDAQ",
            }
            return asset_response
        if path == "/v2/orders":
            return mock_order_response
        raise ValueError(f"Unexpected request: {method} {path}")

    mock_request.side_effect = side_effect

    paper_settings = Settings(
        _env_file=None,
        broker_mode="paper",
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        alpaca_base_url="https://paper-api.alpaca.markets",
        trading_enabled=False,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    paper_broker = create_broker(paper_settings)
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio, settings=paper_settings, broker=paper_broker)
    execution = ExecutionService(
        broker=paper_broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=not paper_settings.trading_enabled,
        settings=paper_settings,
    )

    dry_run_result = execution.process_signal(
        TradeSignal(symbol="AAPL", signal=Signal.BUY, price=100.0, stop_price=95.0)
    )

    assert dry_run_result["action"] == "dry_run"
    assert all(call.args[1] != "/v2/orders" for call in mock_request.call_args_list)

    live_paper_settings = Settings(
        _env_file=None,
        broker_mode="paper",
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        alpaca_base_url="https://paper-api.alpaca.markets",
        trading_enabled=True,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    live_paper_broker = create_broker(live_paper_settings)
    live_portfolio = Portfolio()
    live_risk_manager = RiskManager(live_portfolio, settings=live_paper_settings, broker=live_paper_broker)
    live_execution = ExecutionService(
        broker=live_paper_broker,
        portfolio=live_portfolio,
        risk_manager=live_risk_manager,
        dry_run=not live_paper_settings.trading_enabled,
        settings=live_paper_settings,
    )

    submitted_result = live_execution.process_signal(
        TradeSignal(symbol="AAPL", signal=Signal.BUY, price=100.0, stop_price=95.0)
    )

    assert submitted_result["action"] == "submitted"
    assert any(call.args[1] == "/v2/orders" for call in mock_request.call_args_list)
