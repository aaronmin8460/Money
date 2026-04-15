from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import httpx

from app.config.settings import Settings, get_settings
from app.monitoring.logger import get_logger
from app.news.rss_ingest import NewsHeadline

_TOKEN_PATTERN = re.compile(r"\$?[A-Z]{1,5}(?:/[A-Z]{3})?")
_NON_ALNUM_PATTERN = re.compile(r"[^A-Z0-9]+")
_COMMON_COMPANY_SUFFIXES = {
    "CO",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "INC",
    "INCORPORATED",
    "LTD",
    "LIMITED",
    "PLC",
}

logger = get_logger("news.ticker_mapper")
_DEFAULT_SEC_RESOLVER: "SECCompanyTickerResolver | None" = None


def _normalize_cik(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    digits = re.sub(r"\D+", "", text)
    if not digits:
        return None
    return str(int(digits))


def _normalize_company_name(value: object) -> str:
    text = _NON_ALNUM_PATTERN.sub(" ", str(value or "").upper()).strip()
    tokens = [token for token in text.split() if token]
    while tokens and tokens[-1] in _COMMON_COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass
class SECCompanyTickerResolver:
    by_cik: dict[str, str]
    by_company_name: dict[str, str]

    @classmethod
    def from_payload(cls, payload: object) -> "SECCompanyTickerResolver":
        records: list[dict[str, object]] = []
        if isinstance(payload, dict):
            values = payload.values()
        elif isinstance(payload, list):
            values = payload
        else:
            values = []
        for item in values:
            if isinstance(item, dict):
                records.append(item)
        return cls.from_records(records)

    @classmethod
    def from_records(cls, records: Iterable[dict[str, object]]) -> "SECCompanyTickerResolver":
        by_cik: dict[str, str] = {}
        by_company_name: dict[str, str] = {}
        for record in records:
            ticker = str(record.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            cik = _normalize_cik(record.get("cik_str") or record.get("cik"))
            if cik:
                by_cik[cik] = ticker
                by_cik[cik.zfill(10)] = ticker
            company_name = _normalize_company_name(record.get("title") or record.get("name") or record.get("company_name"))
            if company_name:
                by_company_name[company_name] = ticker
        return cls(by_cik=by_cik, by_company_name=by_company_name)

    @classmethod
    def default(cls, settings: Settings | None = None) -> "SECCompanyTickerResolver":
        global _DEFAULT_SEC_RESOLVER
        if _DEFAULT_SEC_RESOLVER is not None:
            return _DEFAULT_SEC_RESOLVER
        resolved_settings = settings or get_settings()
        _DEFAULT_SEC_RESOLVER = load_sec_company_ticker_resolver(resolved_settings)
        return _DEFAULT_SEC_RESOLVER

    def resolve_cik(self, cik: object) -> str | None:
        normalized = _normalize_cik(cik)
        if not normalized:
            return None
        return self.by_cik.get(normalized) or self.by_cik.get(normalized.zfill(10))

    def resolve_company_name(self, company_name: object) -> str | None:
        normalized = _normalize_company_name(company_name)
        if not normalized:
            return None
        return self.by_company_name.get(normalized)


def _load_sec_reference_from_cache(cache_path: Path, *, ttl_hours: int) -> object | None:
    if not cache_path.exists():
        return None
    if ttl_hours > 0:
        modified_at = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
        if datetime.now(timezone.utc) - modified_at > timedelta(hours=ttl_hours):
            return None
    return json.loads(cache_path.read_text(encoding="utf-8"))


def _load_stale_sec_reference_cache(cache_path: Path) -> object | None:
    if not cache_path.exists():
        return None
    return json.loads(cache_path.read_text(encoding="utf-8"))


def load_sec_company_ticker_resolver(settings: Settings | None = None) -> SECCompanyTickerResolver:
    resolved = settings or get_settings()
    cache_path = Path(resolved.sec_company_tickers_cache_path)
    try:
        cached_payload = _load_sec_reference_from_cache(
            cache_path,
            ttl_hours=resolved.sec_company_tickers_cache_ttl_hours,
        )
        if cached_payload is not None:
            return SECCompanyTickerResolver.from_payload(cached_payload)
    except Exception as exc:
        logger.warning("Failed to load SEC ticker cache %s: %s", cache_path, exc)

    try:
        response = httpx.get(
            resolved.sec_company_tickers_url,
            timeout=resolved.news_fetch_timeout_seconds,
            headers={"User-Agent": resolved.sec_user_agent},
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        return SECCompanyTickerResolver.from_payload(payload)
    except Exception as exc:
        logger.warning("Failed to refresh SEC company ticker reference: %s", exc)
        try:
            stale_payload = _load_stale_sec_reference_cache(cache_path)
            if stale_payload is not None:
                logger.warning("Using stale SEC ticker cache after refresh failure", extra={"cache_path": str(cache_path)})
                return SECCompanyTickerResolver.from_payload(stale_payload)
        except Exception as stale_exc:
            logger.warning("Failed to load stale SEC ticker cache %s: %s", cache_path, stale_exc)
    return SECCompanyTickerResolver(by_cik={}, by_company_name={})


def _explicit_ticker_matches(headline: NewsHeadline, *, known: dict[str, str]) -> list[str]:
    pre_mapped = [symbol.upper() for symbol in getattr(headline, "symbol_candidates", []) if symbol.upper() in known]
    if pre_mapped:
        return sorted(set(pre_mapped))

    title = headline.title.upper()
    summary = headline.summary.upper()
    company_name = str((getattr(headline, "raw_metadata", {}) or {}).get("company_name") or "").upper()
    form_type = str((getattr(headline, "raw_metadata", {}) or {}).get("form_type") or "").upper()
    text = f"{title} {summary} {company_name} {form_type}"
    matched: list[str] = []
    for token in _TOKEN_PATTERN.findall(text):
        normalized = token.lstrip("$").upper()
        if normalized in known:
            matched.append(known[normalized])
    return sorted(set(matched))


def map_headline_to_symbols(
    headline: NewsHeadline,
    *,
    known_symbols: Iterable[str],
    sec_resolver: SECCompanyTickerResolver | None = None,
) -> list[str]:
    known = {symbol.upper(): symbol.upper() for symbol in known_symbols}
    if not known:
        return []

    raw_metadata = getattr(headline, "raw_metadata", {}) or {}
    if str(getattr(headline, "source_type", "")).lower() == "sec":
        resolver = sec_resolver or SECCompanyTickerResolver.default()
        cik_ticker = resolver.resolve_cik(raw_metadata.get("cik"))
        if cik_ticker and cik_ticker.upper() in known:
            return [known[cik_ticker.upper()]]
        company_ticker = resolver.resolve_company_name(raw_metadata.get("company_name"))
        if company_ticker and company_ticker.upper() in known:
            return [known[company_ticker.upper()]]

    return _explicit_ticker_matches(headline, known=known)


def group_headlines_by_symbol(
    headlines: list[NewsHeadline],
    *,
    known_symbols: Iterable[str],
    max_headlines_per_symbol: int,
    sec_resolver: SECCompanyTickerResolver | None = None,
) -> dict[str, list[NewsHeadline]]:
    grouped: dict[str, list[NewsHeadline]] = defaultdict(list)
    for headline in headlines:
        for symbol in map_headline_to_symbols(headline, known_symbols=known_symbols, sec_resolver=sec_resolver):
            if len(grouped[symbol]) >= max_headlines_per_symbol:
                continue
            grouped[symbol].append(headline)
    return dict(grouped)
