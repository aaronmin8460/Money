from app.portfolio.portfolio import Portfolio, Position
from app.risk.risk_manager import RiskManager


def test_risk_manager_allows_disabled_trading_for_dry_run() -> None:
    portfolio = Portfolio()
    manager = RiskManager(portfolio)
    manager.settings.trading_enabled = False

    decision = manager.evaluate_order("AAPL", "BUY", 1.0, 100.0)
    assert decision.approved is True
    assert "dry-run" in decision.reason.lower()


def test_risk_manager_blocks_duplicate_long() -> None:
    portfolio = Portfolio()
    portfolio.positions["AAPL"] = Position(symbol="AAPL", quantity=1.0, entry_price=150.0, side="BUY", current_price=150.0)
    manager = RiskManager(portfolio)
    manager.settings.trading_enabled = True

    decision = manager.evaluate_order("AAPL", "BUY", 1.0, 100.0)
    assert decision.approved is False
    assert "duplicate" in decision.reason.lower()
