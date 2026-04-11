from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config.settings import Settings, get_settings
from app.monitoring.jsonl_store import JsonlStore


class NewsFeatureStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.store = JsonlStore(f"{self.settings.log_dir}/news_features.jsonl")

    def write_feature(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **payload,
        }
        self.store.append(record)
        return record

    def latest_for_symbol(self, symbol: str, *, lookback_hours: int | None = None) -> dict[str, Any] | None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours or self.settings.news_lookback_hours)
        latest: dict[str, Any] | None = None
        for row in self.store.read():
            if str(row.get("symbol", "")).upper() != symbol.upper():
                continue
            recorded_at = _parse_ts(row.get("recorded_at"))
            if recorded_at is not None and recorded_at < cutoff:
                continue
            latest = row
        return latest

    def latest_for_symbols(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        return {
            symbol.upper(): payload
            for symbol in symbols
            if (payload := self.latest_for_symbol(symbol)) is not None
        }


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
