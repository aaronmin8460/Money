from __future__ import annotations

from app.config.settings import Settings
from app.db.models import FillRecord, NormalizedSignalRecord
from app.db.session import SessionLocal
from app.domain.models import AssetClass
from app.execution.execution_service import ExecutionService
from app.portfolio.portfolio import Portfolio, Position
from app.risk.risk_manager import RiskManager
from app.services.broker import PaperBroker
from app.services.market_data import CSVMarketDataService
from app.strategies.base import Signal, TradeSignal


def test_execution_persists_signal_and_fill_records(tmp_path) -> None:
    (tmp_path / "TSTX.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,10,11,9,10.5,2000\n"
        "2024-01-02,10.5,12,10,11.5,2200\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False, min_avg_volume=1, min_dollar_volume=1, min_price=1)
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    execution = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=True,
        market_data_service=market_data,
        settings=settings,
    )

    with SessionLocal() as session:
        session.query(NormalizedSignalRecord).filter(NormalizedSignalRecord.symbol == "TSTX").delete()
        session.query(FillRecord).filter(FillRecord.symbol == "TSTX").delete()
        session.commit()

    result = execution.process_signal(
        TradeSignal(
            symbol="TSTX",
            signal=Signal.BUY,
            price=11.5,
            stop_price=10.5,
            reason="persistence test",
        )
    )

    assert result["action"] == "dry_run"
    with SessionLocal() as session:
        signal_rows = session.query(NormalizedSignalRecord).filter(NormalizedSignalRecord.symbol == "TSTX").all()
        fill_rows = session.query(FillRecord).filter(FillRecord.symbol == "TSTX").all()

    assert signal_rows
    assert fill_rows


def test_sell_preserves_fractional_quantity(tmp_path) -> None:
    """Test that SELL orders preserve fractional quantities for crypto positions."""
    (tmp_path / "BTCUSD.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,40000,41000,39000,40500,100\n"
        "2024-01-02,40500,42000,40000,41500,120\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False, min_avg_volume=1, min_dollar_volume=1, min_price=1)
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    portfolio = Portfolio()
    # Add a fractional crypto position
    portfolio.positions["BTC/USD"] = Position(
        symbol="BTC/USD",
        quantity=0.123456,
        entry_price=40000.0,
        side="long",
        current_price=41500.0,
        asset_class=AssetClass.CRYPTO,
    )
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    execution = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=True,
        market_data_service=market_data,
        settings=settings,
    )

    result = execution.process_signal(
        TradeSignal(
            symbol="BTC/USD",
            signal=Signal.SELL,
            price=41500.0,
            reason="fractional sell test",
        )
    )

    # Check that the proposal quantity preserves the fractional amount
    assert result["proposal"]["quantity"] == 0.123456
    assert result["action"] == "dry_run"


def test_sell_without_tracked_long_is_rejected_in_execution_when_short_selling_disabled(tmp_path) -> None:
    (tmp_path / "AAPL.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,100,101,99,100,10000\n"
        "2024-01-02,100,101,99,100,10000\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=True,
        short_selling_enabled=False,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    execution = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=False,
        market_data_service=market_data,
        settings=settings,
    )

    result = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.SELL,
            asset_class=AssetClass.EQUITY,
            strategy_name="ema_crossover",
            price=100.0,
            reason="bearish crossover",
        )
    )

    assert result["action"] == "rejected"
    assert result["risk"]["rule"] == "no_position_to_sell"
    assert result["proposal"]["quantity"] == 0.0


def test_position_sizing_stays_under_buffered_hard_notional_cap(tmp_path) -> None:
    (tmp_path / "AAPL.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,100,101,99,100,10000\n"
        "2024-01-02,100,101,99,100,10000\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        max_position_notional=10_000.0,
        position_notional_buffer_pct=0.995,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    execution = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=True,
        market_data_service=market_data,
        settings=settings,
    )

    result = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            stop_price=95.0,
            reason="buffer sizing test",
        )
    )

    proposal = result["proposal"]
    notional = proposal["quantity"] * proposal["price"]
    assert result["action"] == "dry_run"
    assert proposal["quantity"] == 99.0
    assert notional < settings.max_position_notional
    assert notional <= settings.effective_max_position_notional
