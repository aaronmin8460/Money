from __future__ import annotations

import json
from collections import Counter
from typing import Any

from app.config.settings import Settings, get_settings
from app.news.rss_ingest import NewsHeadline
from app.monitoring.logger import get_logger

logger = get_logger("news.llm")

_POSITIVE_WORDS = {"beats", "surges", "growth", "upgrade", "record", "strong", "profit", "bullish"}
_NEGATIVE_WORDS = {"misses", "drops", "lawsuit", "cut", "weak", "bearish", "fraud", "downgrade", "loss"}
_RISK_TAGS = {
    "lawsuit": "legal",
    "investigation": "regulatory",
    "downgrade": "analyst_downgrade",
    "fraud": "fraud",
    "layoffs": "labor",
    "tariff": "macro",
}
_SEC_EVENT_FORMS = {"8-K", "10-K", "10-Q", "13D", "13D/A", "SC_13D", "SC_13G", "13G", "13G/A"}
_SEC_CAUTION_FORMS = {"S-1", "S-1/A", "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8"}


def _source_features(headlines: list[NewsHeadline], *, settings: Settings) -> dict[str, Any]:
    source_counts = Counter(headline.source_id for headline in headlines)
    source_diversity_count = len(source_counts)
    benzinga_headline_count = source_counts.get("benzinga", 0)
    sec_forms = [
        str((headline.raw_metadata or {}).get("form_type") or "").upper()
        for headline in headlines
        if headline.source_type == "sec"
    ]
    sec_form_type = sec_forms[0] if sec_forms else None
    sec_event_flag = any(form in _SEC_EVENT_FORMS for form in sec_forms)
    sec_caution_flag = any(form in _SEC_CAUTION_FORMS for form in sec_forms)

    catalyst_score = 0.0
    if benzinga_headline_count:
        catalyst_score += min(0.35, benzinga_headline_count * 0.12)
    if sec_event_flag:
        catalyst_score += 0.28
    if sec_form_type in {"8-K", "13D", "13D/A", "SC_13D"}:
        catalyst_score += 0.15
    if sec_form_type in {"10-K", "10-Q"}:
        catalyst_score += 0.08
    if sec_caution_flag:
        catalyst_score -= 0.18
    if settings.news_enable_source_diversity_features and source_diversity_count >= 2:
        catalyst_score += min(0.18, source_diversity_count * 0.05)

    source_risk_tags: set[str] = set()
    if sec_caution_flag:
        source_risk_tags.add("dilution_risk")
    if sec_event_flag:
        source_risk_tags.add("structured_disclosure")

    return {
        "headline_count_by_source": dict(source_counts),
        "benzinga_headline_count": benzinga_headline_count,
        "source_diversity_count": source_diversity_count,
        "cross_source_confirmation": bool(settings.news_enable_source_diversity_features and source_diversity_count >= 2),
        "sec_event_flag": sec_event_flag,
        "sec_form_type": sec_form_type,
        "sec_caution_flag": sec_caution_flag,
        "catalyst_score": max(0.0, min(1.0, catalyst_score)),
        "source_ids": sorted(source_counts),
        "risk_tags": sorted(source_risk_tags),
    }


def _merge_analysis(
    *,
    base: dict[str, Any],
    source_features: dict[str, Any],
    analysis_mode: str,
    analysis_reason: str,
    llm_status: str,
) -> dict[str, Any]:
    risk_tags = sorted(set(base.get("risk_tags") or []) | set(source_features.get("risk_tags") or []))
    relevance_score = max(
        float(base.get("relevance_score") or 0.0),
        min(1.0, 0.25 + (0.08 * len(source_features.get("headline_count_by_source") or {})) + (source_features.get("catalyst_score") or 0.0) * 0.5),
    )
    return {
        **base,
        "risk_tags": risk_tags,
        "relevance_score": relevance_score,
        "analysis_mode": analysis_mode,
        "analysis_reason": analysis_reason,
        "llm_status": llm_status,
        **source_features,
    }


def _heuristic_analysis(
    symbol: str,
    headlines: list[NewsHeadline],
    *,
    reason: str,
    llm_status: str,
    settings: Settings,
) -> dict[str, Any]:
    text = " ".join(f"{headline.title} {headline.summary}" for headline in headlines).lower()
    source_features = _source_features(headlines, settings=settings)
    positive_hits = sum(word in text for word in _POSITIVE_WORDS)
    negative_hits = sum(word in text for word in _NEGATIVE_WORDS)
    sentiment_score = 0.5
    if positive_hits or negative_hits:
        sentiment_score = max(0.0, min(1.0, 0.5 + ((positive_hits - negative_hits) * 0.1)))
    sentiment_score += min(0.12, float(source_features["catalyst_score"]) * 0.2)
    if source_features["sec_caution_flag"]:
        sentiment_score -= 0.12
    sentiment_score = max(0.0, min(1.0, sentiment_score))

    sentiment_label = "neutral"
    if sentiment_score >= 0.6:
        sentiment_label = "positive"
    elif sentiment_score <= 0.4:
        sentiment_label = "negative"

    risk_tags = sorted({tag for keyword, tag in _RISK_TAGS.items() if keyword in text})
    return _merge_analysis(
        base={
            "symbol": symbol,
            "summary": " | ".join(headline.title for headline in headlines[:3]),
            "sentiment_label": sentiment_label,
            "sentiment_score": sentiment_score,
            "risk_tags": risk_tags,
            "relevance_score": min(1.0, 0.4 + (0.1 * len(headlines))),
        },
        source_features=source_features,
        analysis_mode="heuristic",
        analysis_reason=reason,
        llm_status=llm_status,
    )


def analyze_headlines(
    symbol: str,
    headlines: list[NewsHeadline],
    *,
    settings: Settings | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    if not headlines:
        return {
            "symbol": symbol,
            "summary": "",
            "sentiment_label": "neutral",
            "sentiment_score": 0.5,
            "risk_tags": [],
            "relevance_score": 0.0,
            "analysis_mode": "empty",
            "analysis_reason": "no_headlines",
            "llm_status": resolved_settings.news_llm_status,
            "headline_count_by_source": {},
            "benzinga_headline_count": 0,
            "source_diversity_count": 0,
            "cross_source_confirmation": False,
            "sec_event_flag": False,
            "sec_form_type": None,
            "sec_caution_flag": False,
            "catalyst_score": 0.0,
            "source_ids": [],
        }
    if not resolved_settings.news_llm_available:
        llm_status = resolved_settings.news_llm_status
        logger.info(
            "Using heuristic news analysis",
            extra={
                "symbol": symbol,
                "headline_count": len(headlines),
                "llm_status": llm_status,
                "source_ids": sorted({headline.source_id for headline in headlines}),
            },
        )
        return _heuristic_analysis(
            symbol,
            headlines,
            reason=llm_status,
            llm_status=llm_status,
            settings=resolved_settings,
        )

    prompt = (
        "You are classifying market news for feature engineering only.\n"
        "Treat SEC filings as structured disclosures, not marketing headlines.\n"
        "Do not assume every SEC filing is directional alpha.\n"
        "Return strict JSON with keys: summary, sentiment_label, sentiment_score, risk_tags, relevance_score.\n"
        f"Ticker: {symbol}\n"
        f"Items: {json.dumps([headline.to_dict() for headline in headlines[:resolved_settings.news_max_headlines_per_ticker]])}"
    )
    source_features = _source_features(headlines, settings=resolved_settings)
    try:
        if client is None:
            from openai import OpenAI  # type: ignore[import-not-found]

            client = OpenAI(api_key=resolved_settings.openai_api_key)
        response = client.responses.create(  # type: ignore[attr-defined]
            model=resolved_settings.openai_model,
            input=prompt,
        )
        raw_text = getattr(response, "output_text", "") or ""
        payload = json.loads(raw_text)
        logger.info(
            "OpenAI news analysis completed",
            extra={
                "symbol": symbol,
                "headline_count": len(headlines),
                "openai_model": resolved_settings.openai_model,
                "source_ids": source_features["source_ids"],
            },
        )
        return _merge_analysis(
            base={
                "symbol": symbol,
                "summary": str(payload.get("summary", "")),
                "sentiment_label": str(payload.get("sentiment_label", "neutral")),
                "sentiment_score": float(payload.get("sentiment_score", 0.5)),
                "risk_tags": list(payload.get("risk_tags", [])),
                "relevance_score": float(payload.get("relevance_score", 0.0)),
                "llm_model": resolved_settings.openai_model,
            },
            source_features=source_features,
            analysis_mode="llm",
            analysis_reason="llm_success",
            llm_status=resolved_settings.news_llm_status,
        )
    except Exception as exc:
        logger.warning("OpenAI news analysis failed for %s: %s", symbol, exc)
        return _heuristic_analysis(
            symbol,
            headlines,
            reason="llm_fallback_after_error",
            llm_status=resolved_settings.news_llm_status,
            settings=resolved_settings,
        )
