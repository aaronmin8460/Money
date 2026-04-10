from dataclasses import dataclass

from app.config.settings import Settings
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


@dataclass
class FakeAccount:
    cash: float = 100000.0
    equity: float = 100000.0
    buying_power: float = 100000.0


class FakeBroker:
    def get_account(self) -> FakeAccount:
        return FakeAccount()


def test_risk_manager_blocks_stop_based_risk_when_it_exceeds_budget() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=True,
        max_risk_per_trade=0.01,
        max_position_notional=100000.0,
    )
    portfolio = Portfolio()
    manager = RiskManager(portfolio, settings=settings, broker=FakeBroker())

    decision = manager.evaluate_order("AAPL", "BUY", 100.0, 100.0, stop_price=80.0)

    assert decision.approved is False
    assert "stop-based trade risk" in decision.reason.lower()


def test_risk_manager_distinguishes_notional_limit_from_stop_risk() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=True,
        max_risk_per_trade=0.01,
        max_position_notional=10000.0,
    )
    portfolio = Portfolio()
    manager = RiskManager(portfolio, settings=settings, broker=FakeBroker())

    notional_decision = manager.evaluate_order("AAPL", "BUY", 200.0, 100.0, stop_price=99.0)
    approved_decision = manager.evaluate_order("AAPL", "BUY", 100.0, 100.0, stop_price=99.0)

    assert notional_decision.approved is False
    assert "max position notional" in notional_decision.reason.lower()
    assert approved_decision.approved is True
