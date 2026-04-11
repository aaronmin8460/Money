from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_ISO_DATETIME_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})(?P<fraction>\.\d+)?(?P<tz>[+-]\d{2}:\d{2})?$"
)


def _apply_naive_timezone(value: datetime, assume_utc_when_naive: bool) -> datetime:
    if value.tzinfo is None and assume_utc_when_naive:
        return value.replace(tzinfo=timezone.utc)
    return value


def _normalize_iso_datetime_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return normalized
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"

    match = _ISO_DATETIME_RE.fullmatch(normalized)
    if not match:
        return normalized

    fraction = match.group("fraction")
    if fraction and len(fraction) > 7:
        normalized = f"{match.group('base')}{fraction[:7]}{match.group('tz') or ''}"
    return normalized


def parse_iso_datetime(
    value: Any,
    *,
    default_none: datetime | None = None,
    assume_utc_when_naive: bool = True,
) -> datetime | None:
    if value is None:
        return default_none
    if isinstance(value, datetime):
        return _apply_naive_timezone(value, assume_utc_when_naive)

    normalized = _normalize_iso_datetime_text(str(value))
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO datetime string: {value!r}") from exc
    return _apply_naive_timezone(parsed, assume_utc_when_naive)
