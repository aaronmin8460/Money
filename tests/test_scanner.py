from __future__ import annotations

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
