from app.services.broker import OrderRequest, PaperBroker


def test_paper_broker_submits_dry_run_order() -> None:
    broker = PaperBroker()
    order = OrderRequest(symbol="AAPL", side="BUY", quantity=1.0, price=150.0, is_dry_run=True)
    result = broker.submit_order(order)
    assert result["status"] == "DRY_RUN"
    assert result["symbol"] == "AAPL"


def test_paper_broker_calculates_equity() -> None:
    broker = PaperBroker()
    assert broker.get_account().equity == 100_000.0
