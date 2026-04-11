from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from app.news.rss_ingest import NewsHeadline

_TOKEN_PATTERN = re.compile(r"\$?[A-Z]{1,5}(?:/[A-Z]{3})?")


def map_headline_to_symbols(
    headline: NewsHeadline,
    *,
    known_symbols: Iterable[str],
) -> list[str]:
    known = {symbol.upper(): symbol.upper() for symbol in known_symbols}
    title = headline.title.upper()
    summary = headline.summary.upper()
    text = f"{title} {summary}"
    matched: list[str] = []
    for token in _TOKEN_PATTERN.findall(text):
        normalized = token.lstrip("$").upper()
        if normalized in known:
            matched.append(known[normalized])
    return sorted(set(matched))


def group_headlines_by_symbol(
    headlines: list[NewsHeadline],
    *,
    known_symbols: Iterable[str],
    max_headlines_per_symbol: int,
) -> dict[str, list[NewsHeadline]]:
    grouped: dict[str, list[NewsHeadline]] = defaultdict(list)
    for headline in headlines:
        for symbol in map_headline_to_symbols(headline, known_symbols=known_symbols):
            if len(grouped[symbol]) >= max_headlines_per_symbol:
                continue
            grouped[symbol].append(headline)
    return dict(grouped)
