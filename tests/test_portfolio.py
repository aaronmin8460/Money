from datetime import date, datetime, timezone

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
