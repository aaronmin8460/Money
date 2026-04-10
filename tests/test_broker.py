from app.config.settings import Settings
from app.services.broker import OrderRequest, PaperBroker


def test_paper_broker_submits_dry_run_order() -> None:
    settings = Settings(_env_file=None, broker_mode="mock")
    broker = PaperBroker(settings)
    order = OrderRequest(symbol="AAPL", side="BUY", quantity=1.0, price=150.0, is_dry_run=True)
    result = broker.submit_order(order)
    assert result["status"] == "DRY_RUN"
    assert result["symbol"] == "AAPL"


def test_paper_broker_calculates_equity() -> None:
    settings = Settings(_env_file=None, broker_mode="mock")
    broker = PaperBroker(settings)
    assert broker.get_account().equity == 100_000.0


def test_paper_broker_get_account() -> None:
    settings = Settings(_env_file=None, broker_mode="mock")
    broker = PaperBroker(settings)
    account = broker.get_account()
    assert account.cash == 100_000.0
    assert account.equity == 100_000.0
    assert account.positions == 0
    assert account.buying_power == 100_000.0
    assert account.mode == "mock"
    assert account.trading_enabled is False


def test_paper_broker_market_open_true() -> None:
    settings = Settings(_env_file=None, broker_mode="mock")
    broker = PaperBroker(settings)
    assert broker.is_market_open() is True


def test_paper_broker_reset_state_clears_positions_and_orders() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=True)
    broker = PaperBroker(settings)
    broker.submit_order(OrderRequest(symbol="AAPL", side="BUY", quantity=1.0, price=100.0, is_dry_run=False))

    broker.reset_state(clear_orders=True, clear_positions=True)

    assert broker.get_positions() == []
    assert broker.list_orders() == []
    assert broker.get_account().cash == 100_000.0
