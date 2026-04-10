from __future__ import annotations

from app.config.settings import Settings
from app.db.models import FillRecord, NormalizedSignalRecord
from app.db.session import SessionLocal
from app.execution.execution_service import ExecutionService
from app.portfolio.portfolio import Portfolio
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

    settings = Settings(_env_file=None, broker_mode="paper", trading_enabled=False, min_avg_volume=1, min_dollar_volume=1, min_price=1)
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
