from datetime import datetime, timezone
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


def test_buy_is_rejected_when_daily_loss_limit_is_exceeded() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=True,
        max_daily_loss=100_000.0,
        max_daily_loss_pct=0.02,
    )
    portfolio = Portfolio(cash=97_000.0)
    portfolio.reset_daily_baseline(
        equity=100_000.0,
        as_of=datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc),
    )
    manager = RiskManager(portfolio, settings=settings)

    decision = manager.evaluate_order("AAPL", "BUY", 1.0, 100.0)

    assert decision.approved is False
    assert decision.rule == "daily_loss_pct_limit"


def test_sell_is_allowed_when_daily_loss_limit_is_exceeded_but_exposure_is_reduced() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=True,
        max_daily_loss=1_000.0,
        max_daily_loss_pct=0.02,
    )
    portfolio = Portfolio(cash=95_000.0)
    portfolio.positions["QQQ"] = Position(
        symbol="QQQ",
        quantity=5.0,
        entry_price=300.0,
        side="BUY",
        current_price=200.0,
    )
    portfolio.reset_daily_baseline(
        equity=100_000.0,
        as_of=datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc),
    )
    manager = RiskManager(portfolio, settings=settings)

    decision = manager.evaluate_order("QQQ", "SELL", 2.0, 200.0)

    assert decision.approved is True
    assert decision.rule == "approved"


def test_sell_without_tracked_long_position_is_rejected_when_short_selling_disabled() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=True,
        short_selling_enabled=False,
    )
    portfolio = Portfolio(cash=100_000.0)
    manager = RiskManager(portfolio, settings=settings)

    decision = manager.evaluate_order("TSLA", "SELL", 1.0, 250.0)

    assert decision.approved is False
    assert decision.rule == "no_position_to_sell"
    assert decision.details["has_tracked_position"] is False
    assert decision.details["tracked_position_sellable"] is False
