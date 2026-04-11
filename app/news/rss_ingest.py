from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from app.monitoring.logger import get_logger

logger = get_logger("news.rss")

DEFAULT_RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
]


@dataclass
class NewsHeadline:
    title: str
    summary: str
    source: str
    url: str
    published_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at,
        }


def _parse_published(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_rss_headlines(
    feeds: list[str] | None = None,
    *,
    lookback_hours: int = 24,
    parser: Any | None = None,
) -> list[NewsHeadline]:
    try:
        feedparser = parser
        if feedparser is None:
            import feedparser as feedparser_module  # type: ignore[import-not-found]

            feedparser = feedparser_module
    except Exception as exc:
        logger.warning("RSS ingestion disabled because feedparser is unavailable: %s", exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    headlines: list[NewsHeadline] = []
    for feed_url in feeds or DEFAULT_RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            logger.warning("RSS fetch failed for %s: %s", feed_url, exc)
            continue

        source = getattr(parsed.feed, "title", None) or feed_url
        for entry in getattr(parsed, "entries", []):
            published_at = _parse_published(
                getattr(entry, "published", None) or getattr(entry, "updated", None)
            )
            if published_at and published_at < cutoff:
                continue
            headlines.append(
                NewsHeadline(
                    title=str(getattr(entry, "title", "")).strip(),
                    summary=str(getattr(entry, "summary", "")).strip(),
                    source=str(source),
                    url=str(getattr(entry, "link", "")).strip(),
                    published_at=(published_at or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
                )
            )
    return headlines
