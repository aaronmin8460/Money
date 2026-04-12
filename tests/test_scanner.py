from __future__ import annotations

import pandas as pd

from app.domain.models import AssetClass, AssetMetadata, NormalizedMarketSnapshot
from app.config.settings import Settings
from app.db.models import AssetCatalogEntry, AssetCatalogSyncRun, RankedOpportunityRecord, ScannerRun
from app.db.session import SessionLocal
from app.services.asset_catalog import AssetCatalogService
from app.services.broker import PaperBroker
from app.services.market_data import CSVMarketDataService
from app.services.scanner import ScannerService


def test_scanner_ranks_multi_asset_opportunities(tmp_path) -> None:
    (tmp_path / "AAPL.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,100,101,99,100,2000\n"
        "2024-01-02,100,103,99,102,2200\n"
        "2024-01-03,102,105,101,104,2500\n"
        "2024-01-04,104,108,103,107,2800\n"
        "2024-01-05,107,111,106,110,3200\n",
        encoding="utf-8",
    )
    (tmp_path / "BTCUSD.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,40000,40300,39800,40200,3000\n"
        "2024-01-02,40200,40800,40100,40700,3200\n"
        "2024-01-03,40700,41400,40500,41200,3500\n"
        "2024-01-04,41200,42000,41000,41800,3800\n"
        "2024-01-05,41800,42800,41700,42600,4200\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        scanner_limit_per_asset_class=10,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    catalog = AssetCatalogService(broker=broker, settings=settings)

    with SessionLocal() as session:
        session.query(AssetCatalogEntry).delete()
        session.query(AssetCatalogSyncRun).delete()
        session.query(RankedOpportunityRecord).delete()
        session.query(ScannerRun).delete()
        session.commit()

    catalog.refresh(force=True)
    scanner = ScannerService(asset_catalog=catalog, market_data_service=market_data, settings=settings)
    result = scanner.scan(limit=5)

    assert result.scanned_count == 2
    assert result.opportunities
    assert result.top_gainers
    assert any(item.asset_class.value == "crypto" for item in result.opportunities)
    assert result.top_gainers[0].signal_quality_score >= result.top_gainers[-1].signal_quality_score


def test_scanner_crypto_only_mode_uses_configured_crypto_universe(tmp_path) -> None:
    (tmp_path / "AAPL.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,100,101,99,100,2000\n"
        "2024-01-02,100,103,99,102,2200\n"
        "2024-01-03,102,105,101,104,2500\n"
        "2024-01-04,104,108,103,107,2800\n"
        "2024-01-05,107,111,106,110,3200\n",
        encoding="utf-8",
    )
    (tmp_path / "BTCUSD.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,40000,40300,39800,40200,3000\n"
        "2024-01-02,40200,40800,40100,40700,3200\n"
        "2024-01-03,40700,41400,40500,41200,3500\n"
        "2024-01-04,41200,42000,41000,41800,3800\n"
        "2024-01-05,41800,42800,41700,42600,4200\n",
        encoding="utf-8",
    )
    (tmp_path / "ETHUSD.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,2200,2230,2180,2210,2200\n"
        "2024-01-02,2210,2260,2200,2240,2300\n"
        "2024-01-03,2240,2290,2230,2275,2400\n"
        "2024-01-04,2275,2310,2260,2290,2500\n"
        "2024-01-05,2290,2340,2280,2330,2600\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        crypto_only_mode=True,
        crypto_symbols=["BTC/USD", "ETH/USD"],
        scanner_limit_per_asset_class=10,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    catalog = AssetCatalogService(broker=broker, settings=settings)

    with SessionLocal() as session:
        session.query(AssetCatalogEntry).delete()
        session.query(AssetCatalogSyncRun).delete()
        session.query(RankedOpportunityRecord).delete()
        session.query(ScannerRun).delete()
        session.commit()

    catalog.refresh(force=True)
    scanner = ScannerService(asset_catalog=catalog, market_data_service=market_data, settings=settings)
    result = scanner.scan(limit=5)

    assert result.scanned_count == 2
    assert list(result.symbol_snapshots.keys()) == ["BTC/USD", "ETH/USD"]
    assert "AAPL" not in result.symbol_snapshots


def test_scanner_explicit_symbols_fall_back_when_catalog_is_missing_entries(tmp_path, monkeypatch) -> None:
    (tmp_path / "BTCUSD.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,40000,40300,39800,40200,3000\n"
        "2024-01-02,40200,40800,40100,40700,3200\n"
        "2024-01-03,40700,41400,40500,41200,3500\n"
        "2024-01-04,41200,42000,41000,41800,3800\n"
        "2024-01-05,41800,42800,41700,42600,4200\n",
        encoding="utf-8",
    )
    (tmp_path / "ETHUSD.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-01,2200,2230,2180,2210,2200\n"
        "2024-01-02,2210,2260,2200,2240,2300\n"
        "2024-01-03,2240,2290,2230,2275,2400\n"
        "2024-01-04,2275,2310,2260,2290,2500\n"
        "2024-01-05,2290,2340,2280,2330,2600\n",
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
    catalog = AssetCatalogService(broker=broker, settings=settings)
    scanner = ScannerService(asset_catalog=catalog, market_data_service=market_data, settings=settings)

    monkeypatch.setattr(catalog, "get_asset", lambda _symbol: None)

    result = scanner.scan(symbols=["BTC/USD", "ETH/USD"], limit=5)

    assert result.scanned_count == 2
    assert list(result.symbol_snapshots.keys()) == ["BTC/USD", "ETH/USD"]


def test_scanner_uses_asset_class_specific_intraday_timeframes(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
        scanner_timeframe_by_asset_class={"equity": "15Min", "crypto": "15Min"},
        lookback_bars_by_asset_class={"equity": 42, "crypto": 64},
    )
    fetch_calls: list[tuple[str, str, int]] = []

    class StubCatalog:
        def get_asset(self, symbol: str):
            return {
                "AAPL": AssetMetadata(symbol="AAPL", name="AAPL", asset_class=AssetClass.EQUITY, exchange="NASDAQ"),
                "BTC/USD": AssetMetadata(symbol="BTC/USD", name="BTC/USD", asset_class=AssetClass.CRYPTO, exchange="CRYPTO"),
            }.get(symbol)

        def get_scan_universe(self, asset_class=None):
            return []

    class StubMarketData:
        def get_normalized_snapshot(self, symbol: str, asset_class: AssetClass):
            price = 150.0 if asset_class == AssetClass.EQUITY else 60_000.0
            return NormalizedMarketSnapshot(
                symbol=symbol,
                asset_class=asset_class,
                last_trade_price=price,
                evaluation_price=price,
                quote_available=True,
                quote_stale=False,
                price_source_used="last_trade",
                session_state="regular" if asset_class != AssetClass.CRYPTO else "always_open",
                exchange="MOCK",
                source="mock",
            )

        def fetch_bars(self, symbol: str, timeframe: str | None = None, limit: int = 50, asset_class=None):
            fetch_calls.append((symbol, str(timeframe), int(limit)))
            return pd.DataFrame(
                {
                    "Open": [100, 101, 102, 103, 104, 105],
                    "High": [101, 102, 103, 104, 105, 106],
                    "Low": [99, 100, 101, 102, 103, 104],
                    "Close": [100, 101, 102, 103, 104, 105],
                    "Volume": [1000, 1100, 1200, 1300, 1400, 1500],
                }
            )

    scanner = ScannerService(asset_catalog=StubCatalog(), market_data_service=StubMarketData(), settings=settings)
    result = scanner.scan(symbols=["AAPL", "BTC/USD"], limit=5)

    assert result.scanned_count == 2
    assert ("AAPL", "15Min", 42) in fetch_calls
    assert ("BTC/USD", "15Min", 64) in fetch_calls
    assert result.timeframes_by_asset_class["equity"]["scanner_timeframe"] == "15Min"


def test_scanner_prefilter_ranks_beyond_catalog_order(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
        universe_prefilter_limit_by_asset_class={"equity": 3},
        final_evaluation_limit_by_asset_class={"equity": 2},
        scanner_timeframe_by_asset_class={"equity": "15Min"},
        lookback_bars_by_asset_class={"equity": 40},
    )
    assets = [
        AssetMetadata(symbol="AAA", name="AAA", asset_class=AssetClass.EQUITY, exchange="NASDAQ"),
        AssetMetadata(symbol="BBB", name="BBB", asset_class=AssetClass.EQUITY, exchange="NASDAQ"),
        AssetMetadata(symbol="ZZZ", name="ZZZ", asset_class=AssetClass.EQUITY, exchange="NASDAQ"),
    ]
    score_by_symbol = {"AAA": 100_000.0, "BBB": 250_000.0, "ZZZ": 2_000_000.0}

    class StubCatalog:
        def get_asset(self, symbol: str):
            return next((asset for asset in assets if asset.symbol == symbol), None)

        def get_scan_universe(self, asset_class=None):
            return list(assets)

    class StubMarketData:
        def get_normalized_snapshot(self, symbol: str, asset_class: AssetClass):
            price = 100.0 + len(symbol)
            return NormalizedMarketSnapshot(
                symbol=symbol,
                asset_class=asset_class,
                last_trade_price=price,
                evaluation_price=price,
                quote_available=True,
                quote_stale=False,
                price_source_used="last_trade",
                session_state="regular",
                exchange="MOCK",
                source="mock",
            )

        def fetch_bars(self, symbol: str, timeframe: str | None = None, limit: int = 50, asset_class=None):
            volume_base = score_by_symbol[symbol] / 100.0
            closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
            return pd.DataFrame(
                {
                    "Open": closes,
                    "High": [value + 1 for value in closes],
                    "Low": [value - 1 for value in closes],
                    "Close": closes,
                    "Volume": [volume_base * factor for factor in [0.8, 0.9, 1.0, 1.1, 1.2, 1.3]],
                }
            )

    scanner = ScannerService(asset_catalog=StubCatalog(), market_data_service=StubMarketData(), settings=settings)
    result = scanner.scan(asset_class=AssetClass.EQUITY, limit=5)

    assert result.prefilter_counts["equity"] == 3
    assert result.final_evaluation_counts["equity"] == 2
    assert "ZZZ" in result.symbol_snapshots
    assert "AAA" not in result.symbol_snapshots
