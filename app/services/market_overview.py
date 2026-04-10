from __future__ import annotations

from typing import Any

from app.domain.models import AssetClass
from app.services.scanner import ScannerService


class MarketOverviewService:
    def __init__(self, scanner: ScannerService):
        self.scanner = scanner

    def get_overview(self, asset_class: AssetClass | str | None = None, limit: int = 10) -> dict[str, Any]:
        result = self.scanner.scan(asset_class=asset_class, limit=limit)
        payload = result.to_dict()
        payload["summary"] = {
            "opportunity_count": len(result.opportunities),
            "top_symbols": [item.symbol for item in result.opportunities[: min(5, len(result.opportunities))]],
        }
        return payload
