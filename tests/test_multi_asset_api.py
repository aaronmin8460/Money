from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import app
from app.config import settings as settings_module
from app.config.settings import Settings


def test_multi_asset_endpoints_return_data() -> None:
    settings_module._settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        auto_trade_enabled=False,
        default_symbols=["AAPL", "BTC/USD"],
        min_avg_volume=1,
        min_dollar_volume=1,
        min_price=1,
    )

    with TestClient(app) as client:
        assets_response = client.get("/assets/stats")
        search_response = client.get("/assets/search", params={"q": "BTC"})
        snapshot_response = client.get("/market/snapshot", params={"symbol": "BTC/USD", "asset_class": "crypto"})
        scanner_response = client.get("/scanner/overview", params={"limit": 5})

    assert assets_response.status_code == 200
    assert assets_response.json()["crypto"] >= 1
    assert search_response.status_code == 200
    assert any(item["asset_class"] == "crypto" for item in search_response.json())
    assert snapshot_response.status_code == 200
    assert snapshot_response.json()["asset_class"] == "crypto"
    assert scanner_response.status_code == 200
    assert scanner_response.json()["summary"]["opportunity_count"] >= 1
