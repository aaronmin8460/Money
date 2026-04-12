from app.config.settings import Settings
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

    manager = ExitManager(
        portfolio,
        settings=Settings(_env_file=None, broker_mode="mock", trading_enabled=False),
    )
    evaluation = manager.evaluate_long_position("AAPL", 119.0, asset_class=AssetClass.EQUITY)

    assert evaluation.signal is not None
    assert evaluation.signal.order_intent == "long_exit"
    assert evaluation.signal.exit_stage == "trail"
    assert evaluation.signal.position_size == 2.5
    assert evaluation.state["current_stop"] is not None
    assert evaluation.state["current_stop"] >= 120.0


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

    manager = ExitManager(
        portfolio,
        settings=Settings(_env_file=None, broker_mode="mock", trading_enabled=False),
    )
    evaluation = manager.evaluate_long_position("MSFT", 94.0, asset_class=AssetClass.EQUITY)

    assert evaluation.signal is not None
    assert evaluation.signal.exit_stage == "stop"
    assert evaluation.signal.position_size == 6.0
    assert evaluation.signal.reduce_only is True


def test_exit_manager_generates_partial_take_profit_signal() -> None:
    portfolio = Portfolio()
    portfolio.positions["NVDA"] = Position(
        symbol="NVDA",
        quantity=20.0,
        entry_price=100.0,
        side="BUY",
        current_price=106.0,
        asset_class=AssetClass.EQUITY,
        initial_quantity=20.0,
        highest_price_since_entry=106.0,
        current_stop=95.0,
        entry_signal_metadata={
            "strategy_name": "equity_momentum_breakout",
            "stop_price": 95.0,
            "target_price": 110.0,
            "atr": 5.0,
            "entry_scan_bar_index": 1,
        },
    )
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        partial_take_profit_levels=[1.0, 2.0],
        partial_take_profit_fractions=[0.25, 1.0],
    )
    manager = ExitManager(portfolio, settings=settings)

    evaluation = manager.evaluate_long_position("NVDA", 105.0, asset_class=AssetClass.EQUITY, current_bar_index=2)

    assert evaluation.signal is not None
    assert evaluation.signal.exit_stage == "tp1"
    assert evaluation.signal.exit_fraction == 0.25
    assert evaluation.signal.position_size == 5.0


def test_hard_stop_remains_authoritative_even_with_exit_model_signal() -> None:
    portfolio = Portfolio()
    portfolio.positions["META"] = Position(
        symbol="META",
        quantity=10.0,
        entry_price=100.0,
        side="BUY",
        current_price=100.0,
        asset_class=AssetClass.EQUITY,
        initial_quantity=10.0,
        highest_price_since_entry=103.0,
        current_stop=95.0,
        entry_signal_metadata={
            "strategy_name": "equity_momentum_breakout",
            "stop_price": 95.0,
            "target_price": 112.0,
            "atr": 5.0,
            "entry_scan_bar_index": 1,
        },
    )
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        exit_model_enabled=True,
        ml_exit_min_score=0.55,
    )
    manager = ExitManager(portfolio, settings=settings)

    evaluation = manager.evaluate_long_position(
        "META",
        94.0,
        asset_class=AssetClass.EQUITY,
        current_bar_index=5,
        exit_model_score=0.9,
    )

    assert evaluation.signal is not None
    assert evaluation.signal.exit_stage == "stop"
    assert evaluation.signal.exit_fraction == 1.0


def test_exit_manager_can_choose_full_time_stop_exit() -> None:
    portfolio = Portfolio()
    portfolio.positions["AAPL"] = Position(
        symbol="AAPL",
        quantity=8.0,
        entry_price=100.0,
        side="BUY",
        current_price=100.2,
        asset_class=AssetClass.EQUITY,
        initial_quantity=8.0,
        highest_price_since_entry=101.0,
        current_stop=95.0,
        entry_signal_metadata={
            "strategy_name": "equity_momentum_breakout",
            "stop_price": 95.0,
            "target_price": 110.0,
            "atr": 5.0,
            "entry_scan_bar_index": 1,
        },
    )
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        time_stop_bars=3,
    )
    manager = ExitManager(portfolio, settings=settings)

    evaluation = manager.evaluate_long_position(
        "AAPL",
        99.8,
        asset_class=AssetClass.EQUITY,
        current_bar_index=5,
    )

    assert evaluation.signal is not None
    assert evaluation.signal.exit_stage == "time_stop"
    assert evaluation.signal.exit_fraction == 1.0
