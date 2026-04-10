from __future__ import annotations

from app.config.settings import Settings
from app.db.models import AssetCatalogEntry, AssetCatalogSyncRun
from app.db.session import SessionLocal
from app.services.asset_catalog import AssetCatalogService
from app.services.broker import PaperBroker
from app.services.market_data import CSVMarketDataService


def test_asset_catalog_sync_persists_assets(tmp_path) -> None:
    (tmp_path / "AAPL.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n2024-01-01,1,2,1,2,1000\n",
        encoding="utf-8",
    )
    (tmp_path / "BTCUSD.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n2024-01-01,100,110,95,105,2500\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=None, broker_mode="paper", trading_enabled=False)
    market_data = CSVMarketDataService(data_dir=tmp_path)
    broker = PaperBroker(settings=settings, market_data_service=market_data)
    service = AssetCatalogService(broker=broker, settings=settings)

    with SessionLocal() as session:
        session.query(AssetCatalogEntry).delete()
        session.query(AssetCatalogSyncRun).delete()
        session.commit()

    result = service.refresh(force=True)

    assert result.asset_count == 2
    stats = service.get_stats()
    assert stats["total_assets"] == 2
    assert stats["equities"] == 1
    assert stats["crypto"] == 1

    btc = service.get_asset("BTC/USD")
    assert btc is not None
    assert btc.asset_class.value == "crypto"
