from __future__ import annotations

import json

import pytest

from app.config.settings import Settings
from app.db.init_db import init_db
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

    init_db()
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


def test_crypto_buy_uses_gtc_time_in_force(tmp_path) -> None:
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
            signal=Signal.BUY,
            asset_class=AssetClass.CRYPTO,
            strategy_name="crypto_momentum_trend",
            price=41500.0,
            stop_price=40000.0,
            reason="crypto tif test",
            metrics={"exchange": "CRYPTO"},
        )
    )

    assert result["proposal"]["time_in_force"] == "gtc"
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
    assert proposal["quantity"] == 39.0
    assert notional < settings.max_position_notional
    assert notional <= settings.effective_max_position_notional
    assert result["risk"]["details"]["final_submitted_notional"] <= settings.max_position_notional


def test_final_submitted_notional_is_clamped_after_rounding(tmp_path) -> None:
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
        position_notional_buffer_pct=1.0,
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
            position_size=100.0,
            price=100.0001,
            stop_price=95.0,
            reason="clamp test",
        )
    )

    details = result["risk"]["details"]
    assert result["action"] == "dry_run"
    assert details["quantity_reduced_to_fit_cap"] is True
    assert details["final_submitted_notional"] <= settings.max_position_notional
    assert result["proposal"]["quantity"] == 99.0


def test_scale_in_tranches_progress_and_do_not_repeat_first_tranche(tmp_path) -> None:
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
        entry_tranches=3,
        entry_tranche_weights=[0.4, 0.3, 0.3],
        scale_in_mode="confirmation",
        max_positions_total=1,
        cooldown_seconds_per_symbol=0,
        cooldown_seconds_per_strategy=0,
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

    first = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            stop_price=95.0,
            reason="first tranche",
        )
    )
    second = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            stop_price=95.0,
            reason="second tranche",
        )
    )
    third = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            stop_price=95.0,
            reason="third tranche",
        )
    )
    fourth = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            stop_price=95.0,
            reason="fourth attempt",
        )
    )

    assert first["action"] == "submitted"
    assert second["action"] == "submitted"
    assert third["action"] == "submitted"
    assert first["risk"]["details"]["tranche_number"] == 1
    assert second["risk"]["details"]["tranche_number"] == 2
    assert third["risk"]["details"]["tranche_number"] == 3
    assert fourth["action"] == "rejected"
    assert fourth["risk"]["rule"] == "tranche_plan_completed"


def test_time_mode_blocks_second_tranche_until_wait_rules_are_met(tmp_path) -> None:
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
        entry_tranches=3,
        entry_tranche_weights=[0.4, 0.3, 0.3],
        scale_in_mode="time",
        minutes_between_tranches=10,
        min_bars_between_tranches=1,
        cooldown_seconds_per_symbol=0,
        cooldown_seconds_per_strategy=0,
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

    first = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            reason="first tranche",
        )
    )
    second = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            reason="blocked second tranche",
        )
    )

    assert first["action"] == "submitted"
    assert second["action"] == "rejected"
    assert second["risk"]["rule"] in {"tranche_time_wait", "tranche_bar_wait"}


def test_duplicate_buy_without_tranche_plan_is_blocked(tmp_path) -> None:
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
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    portfolio = Portfolio()
    portfolio.positions["AAPL"] = Position(
        symbol="AAPL",
        quantity=10.0,
        entry_price=100.0,
        side="BUY",
        current_price=100.0,
        asset_class=AssetClass.EQUITY,
    )
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
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            reason="duplicate buy",
        )
    )

    assert result["action"] == "rejected"
    assert result["risk"]["rule"] == "duplicate_position"


def test_add_on_tranches_do_not_consume_new_symbol_slots(tmp_path) -> None:
    (tmp_path / "AAPL.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,100,101,99,100,10000\n"
        "2024-01-02,100,101,99,100,10000\n",
        encoding="utf-8",
    )
    (tmp_path / "MSFT.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,200,201,199,200,10000\n"
        "2024-01-02,200,201,199,200,10000\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=True,
        max_positions_total=1,
        entry_tranches=3,
        entry_tranche_weights=[0.4, 0.3, 0.3],
        scale_in_mode="confirmation",
        cooldown_seconds_per_symbol=0,
        cooldown_seconds_per_strategy=0,
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

    first_aapl = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            reason="first AAPL tranche",
        )
    )
    second_aapl = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=100.0,
            reason="second AAPL tranche",
        )
    )
    first_msft = execution.process_signal(
        TradeSignal(
            symbol="MSFT",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            price=200.0,
            reason="new symbol should fail slot limit",
        )
    )

    assert first_aapl["action"] == "submitted"
    assert second_aapl["action"] == "submitted"
    assert second_aapl["risk"]["details"]["tranche_consumes_new_slot"] is False
    assert first_msft["action"] == "rejected"
    assert first_msft["risk"]["rule"] == "position_count"


def test_persisted_order_metadata_includes_order_intent_and_exit_stage(tmp_path) -> None:
    (tmp_path / "AAPL.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,100,101,99,100,10000\n"
        "2024-01-02,110,111,109,110,10000\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    portfolio = Portfolio()
    portfolio.positions["AAPL"] = Position(
        symbol="AAPL",
        quantity=10.0,
        entry_price=100.0,
        side="BUY",
        current_price=110.0,
        asset_class=AssetClass.EQUITY,
        initial_quantity=10.0,
        highest_price_since_entry=110.0,
        current_stop=100.0,
        tp1_hit=False,
        tp2_hit=False,
        entry_signal_metadata={
            "strategy_name": "equity_momentum_breakout",
            "stop_price": 95.0,
            "target_price": 120.0,
        },
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

    init_db()
    with SessionLocal() as session:
        session.query(FillRecord).filter(FillRecord.symbol == "AAPL").delete()
        session.query(NormalizedSignalRecord).filter(NormalizedSignalRecord.symbol == "AAPL").delete()
        session.commit()

    result = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.SELL,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            signal_type="exit",
            order_intent="long_exit",
            reduce_only=True,
            exit_stage="tp1",
            position_size=5.0,
            price=110.0,
            entry_price=110.0,
            reason="Take first profit target",
        )
    )

    assert result["action"] == "dry_run"
    with SessionLocal() as session:
        fill_row = session.query(FillRecord).filter(FillRecord.symbol == "AAPL").one()
        signal_row = session.query(NormalizedSignalRecord).filter(NormalizedSignalRecord.symbol == "AAPL").one()

    raw_payload = json.loads(fill_row.raw_payload)
    signal_metrics = json.loads(signal_row.metrics_json)

    assert raw_payload["metadata"]["order_intent"] == "long_exit"
    assert raw_payload["metadata"]["position_direction"] == "long"
    assert raw_payload["metadata"]["exit_stage"] == "tp1"
    assert raw_payload["metadata"]["reduce_only"] is True
    assert signal_metrics["order_intent"] == "long_exit"
    assert signal_metrics["position_direction"] == "long"
    assert signal_metrics["exit_stage"] == "tp1"


def test_execution_maps_short_exit_to_buy_and_preserves_partial_cover_size(tmp_path) -> None:
    (tmp_path / "TSLA.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,100,101,99,100,10000\n"
        "2024-01-02,90,91,89,90,10000\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        short_selling_enabled=True,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    portfolio = Portfolio()
    portfolio.positions["TSLA"] = Position(
        symbol="TSLA",
        quantity=5.0,
        entry_price=100.0,
        side="SHORT",
        current_price=90.0,
        asset_class=AssetClass.EQUITY,
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
            symbol="TSLA",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="ema_crossover",
            signal_type="exit",
            order_intent="short_exit",
            reduce_only=True,
            position_size=2.0,
            price=90.0,
            entry_price=90.0,
            reason="Partial short cover",
        )
    )

    assert result["action"] == "dry_run"
    assert result["proposal"]["side"] == "BUY"
    assert result["proposal"]["quantity"] == 2.0
    assert result["proposal"]["order_intent"] == "short_exit"
    assert result["proposal"]["position_direction"] == "short"


def test_meaningful_reduce_only_long_exit_still_submits_normally() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    broker = PaperBroker(settings=settings)
    portfolio = Portfolio()
    portfolio.positions["AAPL"] = Position(
        symbol="AAPL",
        quantity=3.0,
        entry_price=100.0,
        side="BUY",
        current_price=110.0,
        asset_class=AssetClass.EQUITY,
    )
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    execution = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=True,
        settings=settings,
    )

    result = execution.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.SELL,
            asset_class=AssetClass.EQUITY,
            strategy_name="equity_momentum_breakout",
            signal_type="exit",
            order_intent="long_exit",
            reduce_only=True,
            position_size=2.0,
            price=110.0,
            entry_price=110.0,
            reason="Meaningful partial exit",
        )
    )

    assert result["action"] == "dry_run"
    assert result["proposal"]["quantity"] == 2.0
    assert result["risk"]["rule"] == "dry_run"
    assert result["proposal"]["dust_resolution"] == {}


def test_reduce_only_crypto_exit_dust_is_resolved_locally() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    broker = PaperBroker(settings=settings)
    portfolio = Portfolio()
    portfolio.positions["ETH/USD"] = Position(
        symbol="ETH/USD",
        quantity=0.00000075,
        entry_price=1800.0,
        side="BUY",
        current_price=2000.0,
        asset_class=AssetClass.CRYPTO,
    )
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    execution = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=True,
        settings=settings,
    )
    execution.tranche_state.upsert_plan(
        symbol="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        target_position_notional=500.0,
        tranche_weights=[1.0],
        scale_in_mode="confirmation",
        allow_average_down=False,
        decision_reason="Dust exit test",
    )

    result = execution.process_signal(
        TradeSignal(
            symbol="ETH/USD",
            signal=Signal.SELL,
            asset_class=AssetClass.CRYPTO,
            strategy_name="crypto_momentum_trend",
            signal_type="exit",
            order_intent="long_exit",
            reduce_only=True,
            price=2000.0,
            entry_price=2000.0,
            reason="Dust cleanup",
        )
    )

    assert result["action"] == "dust_resolved"
    assert result["risk"]["rule"] == "dust_resolved"
    assert result["risk"]["approved"] is True
    assert result["risk"]["details"]["rounded_quantity"] == 0.0
    assert result["risk"]["details"]["tracked_position_quantity"] == pytest.approx(0.00000075)
    assert result["risk"]["details"]["dust_auto_resolved"] is True
    assert result["risk"]["rule"] != "input_validation"
    assert result["proposal"]["dust_resolution"]["resolution_source"] == "reduce_only_exit"
    assert portfolio.get_position("ETH/USD") is None
    assert execution.tranche_state.get_plan("ETH/USD") is not None
    assert execution.tranche_state.get_plan("ETH/USD").plan_status == "closed"
    assert broker.orders == []


def test_non_dust_unexecutable_exit_is_rejected_without_closing_position() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    broker = PaperBroker(settings=settings)
    portfolio = Portfolio()
    portfolio.positions["ETH/USD"] = Position(
        symbol="ETH/USD",
        quantity=0.00000075,
        entry_price=1_900_000.0,
        side="BUY",
        current_price=2_000_000.0,
        asset_class=AssetClass.CRYPTO,
    )
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    execution = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=True,
        settings=settings,
    )
    execution.tranche_state.upsert_plan(
        symbol="ETH/USD",
        asset_class=AssetClass.CRYPTO,
        target_position_notional=500.0,
        tranche_weights=[1.0],
        scale_in_mode="confirmation",
        allow_average_down=False,
        decision_reason="Non-dust exit test",
    )

    result = execution.process_signal(
        TradeSignal(
            symbol="ETH/USD",
            signal=Signal.SELL,
            asset_class=AssetClass.CRYPTO,
            strategy_name="crypto_momentum_trend",
            signal_type="exit",
            order_intent="long_exit",
            reduce_only=True,
            price=2_000_000.0,
            entry_price=2_000_000.0,
            reason="Unexecutable but not dust",
        )
    )

    latest_rejection = risk_manager.get_rejection_snapshot(limit=1)["latest"]

    assert result["action"] == "rejected"
    assert result["risk"]["rule"] == "non_dust_exit_unexecutable"
    assert result["risk"]["rule"] != "input_validation"
    assert result["risk"]["details"]["unexecutable_exit_reason"] == "exit_qty_rounds_to_zero"
    assert result["risk"]["details"]["tracked_position_abs_market_value"] == pytest.approx(1.5)
    assert portfolio.get_position("ETH/USD") is not None
    assert execution.tranche_state.get_plan("ETH/USD") is not None
    assert execution.tranche_state.get_plan("ETH/USD").plan_status == "active"
    assert "non_dust_exit_unexecutable" in (execution.tranche_state.get_plan("ETH/USD").blocked_reason or "")
    assert latest_rejection is not None
    assert latest_rejection["rule"] == "non_dust_exit_unexecutable"
    assert broker.orders == []
