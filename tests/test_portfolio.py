from datetime import date, datetime, timezone

from app.domain.models import AssetClass
from app.portfolio.portfolio import Portfolio


def test_daily_baseline_resets_on_new_utc_day() -> None:
    portfolio = Portfolio(cash=100_000.0)
    portfolio.reset_daily_baseline(
        equity=100_000.0,
        as_of=datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc),
    )

    portfolio.cash = 98_000.0
    assert portfolio.current_daily_loss_pct(
        as_of=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
    ) == 0.02

    portfolio.cash = 97_500.0
    assert portfolio.current_daily_loss_pct(
        as_of=datetime(2026, 4, 11, 0, 0, 1, tzinfo=timezone.utc),
    ) == 0.0
    assert portfolio.daily_baseline_equity == 97_500.0
    assert portfolio.daily_baseline_date == date(2026, 4, 11)


def test_tp1_partial_exit_reduces_quantity_and_preserves_remaining_position() -> None:
    portfolio = Portfolio(cash=100_000.0)

    portfolio.update_position(
        "AAPL",
        "BUY",
        10.0,
        100.0,
        asset_class=AssetClass.EQUITY,
        order_intent="long_entry",
        signal_metadata={
            "strategy_name": "equity_momentum_breakout",
            "stop_price": 95.0,
            "target_price": 120.0,
            "trailing_stop": 90.0,
        },
    )
    portfolio.update_position(
        "AAPL",
        "SELL",
        5.0,
        110.0,
        asset_class=AssetClass.EQUITY,
        order_intent="long_exit",
        reduce_only=True,
        exit_stage="tp1",
        signal_metadata={"next_stop": 100.0},
    )

    position = portfolio.get_position("AAPL")

    assert position is not None
    assert position.quantity == 5.0
    assert position.initial_quantity == 10.0
    assert position.tp1_hit is True
    assert position.tp2_hit is False
    assert position.current_stop == 100.0


def test_hard_stop_exit_removes_full_remaining_quantity() -> None:
    portfolio = Portfolio(cash=100_000.0)

    portfolio.update_position(
        "QQQ",
        "BUY",
        4.0,
        100.0,
        asset_class=AssetClass.ETF,
        order_intent="long_entry",
        signal_metadata={"stop_price": 95.0, "target_price": 115.0},
    )
    portfolio.update_position(
        "QQQ",
        "SELL",
        4.0,
        94.0,
        asset_class=AssetClass.ETF,
        order_intent="long_exit",
        reduce_only=True,
        exit_stage="stop",
        signal_metadata={"current_stop": 95.0},
    )

    assert portfolio.get_position("QQQ") is None
