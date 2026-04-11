from app.domain.models import AssetClass
from app.portfolio.portfolio import Portfolio, Position
from app.services.exit_manager import ExitManager


def test_trailing_stop_exit_uses_remaining_quantity() -> None:
    portfolio = Portfolio()
    portfolio.positions["AAPL"] = Position(
        symbol="AAPL",
        quantity=2.5,
        entry_price=100.0,
        side="BUY",
        current_price=130.0,
        asset_class=AssetClass.EQUITY,
        initial_quantity=10.0,
        highest_price_since_entry=130.0,
        current_stop=110.0,
        tp1_hit=True,
        tp2_hit=True,
        entry_signal_metadata={
            "strategy_name": "equity_momentum_breakout",
            "stop_price": 95.0,
            "target_price": 120.0,
            "trailing_stop": 90.0,
        },
    )

    manager = ExitManager(portfolio)
    evaluation = manager.evaluate_long_position("AAPL", 119.0, asset_class=AssetClass.EQUITY)

    assert evaluation.signal is not None
    assert evaluation.signal.order_intent == "long_exit"
    assert evaluation.signal.exit_stage == "trail"
    assert evaluation.signal.position_size == 2.5
    assert evaluation.state["current_stop"] == 120.0


def test_exit_manager_generates_full_stop_exit_signal() -> None:
    portfolio = Portfolio()
    portfolio.positions["MSFT"] = Position(
        symbol="MSFT",
        quantity=6.0,
        entry_price=100.0,
        side="BUY",
        current_price=100.0,
        asset_class=AssetClass.EQUITY,
        initial_quantity=6.0,
        highest_price_since_entry=103.0,
        current_stop=95.0,
        entry_signal_metadata={
            "strategy_name": "equity_momentum_breakout",
            "stop_price": 95.0,
            "target_price": 118.0,
        },
    )

    manager = ExitManager(portfolio)
    evaluation = manager.evaluate_long_position("MSFT", 94.0, asset_class=AssetClass.EQUITY)

    assert evaluation.signal is not None
    assert evaluation.signal.exit_stage == "stop"
    assert evaluation.signal.position_size == 6.0
    assert evaluation.signal.reduce_only is True
