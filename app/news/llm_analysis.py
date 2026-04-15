from __future__ import annotations

import json
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


def _heuristic_analysis(
    symbol: str,
    headlines: list[NewsHeadline],
    *,
    reason: str,
    llm_status: str,
) -> dict[str, Any]:
    text = " ".join(f"{headline.title} {headline.summary}" for headline in headlines).lower()
    positive_hits = sum(word in text for word in _POSITIVE_WORDS)
    negative_hits = sum(word in text for word in _NEGATIVE_WORDS)
    sentiment_score = 0.5
    if positive_hits or negative_hits:
        sentiment_score = max(0.0, min(1.0, 0.5 + ((positive_hits - negative_hits) * 0.1)))
    sentiment_label = "neutral"
    if sentiment_score >= 0.6:
        sentiment_label = "positive"
    elif sentiment_score <= 0.4:
        sentiment_label = "negative"
    risk_tags = sorted({tag for keyword, tag in _RISK_TAGS.items() if keyword in text})
    return {
        "symbol": symbol,
        "summary": " | ".join(headline.title for headline in headlines[:3]),
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
        "risk_tags": risk_tags,
        "relevance_score": min(1.0, 0.4 + (0.1 * len(headlines))),
        "analysis_mode": "heuristic",
        "analysis_reason": reason,
        "llm_status": llm_status,
    }


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
        }
    if not resolved_settings.news_llm_available:
        llm_status = resolved_settings.news_llm_status
        logger.info(
            "Using heuristic news analysis",
            extra={
                "symbol": symbol,
                "headline_count": len(headlines),
                "llm_status": llm_status,
            },
        )
        return _heuristic_analysis(
            symbol,
            headlines,
            reason=llm_status,
            llm_status=llm_status,
        )

    prompt = (
        "You are classifying market news for feature engineering only.\n"
        "Return strict JSON with keys: summary, sentiment_label, sentiment_score, risk_tags, relevance_score.\n"
        f"Ticker: {symbol}\n"
        f"Headlines: {json.dumps([headline.to_dict() for headline in headlines[:resolved_settings.news_max_headlines_per_ticker]])}"
    )
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
            },
        )
        return {
            "symbol": symbol,
            "summary": str(payload.get("summary", "")),
            "sentiment_label": str(payload.get("sentiment_label", "neutral")),
            "sentiment_score": float(payload.get("sentiment_score", 0.5)),
            "risk_tags": list(payload.get("risk_tags", [])),
            "relevance_score": float(payload.get("relevance_score", 0.0)),
            "analysis_mode": "llm",
            "analysis_reason": "llm_success",
            "llm_status": resolved_settings.news_llm_status,
            "llm_model": resolved_settings.openai_model,
        }
    except Exception as exc:
        logger.warning("OpenAI news analysis failed for %s: %s", symbol, exc)
        return _heuristic_analysis(
            symbol,
            headlines,
            reason="llm_fallback_after_error",
            llm_status=resolved_settings.news_llm_status,
        )
