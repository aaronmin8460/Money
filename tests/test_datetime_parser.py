from datetime import datetime, timedelta, timezone

import pytest

from app.domain.models import NormalizedMarketSnapshot
from app.utils.datetime_parser import parse_iso_datetime


def test_parse_iso_datetime_truncates_nanosecond_fraction_with_offset() -> None:
    parsed = parse_iso_datetime("2026-04-11T17:24:46.344345659+00:00")

    assert parsed == datetime(2026, 4, 11, 17, 24, 46, 344345, tzinfo=timezone.utc)


def test_parse_iso_datetime_truncates_another_nanosecond_fraction_with_offset() -> None:
    parsed = parse_iso_datetime("2026-04-10T19:59:56.248717591+00:00")

    assert parsed == datetime(2026, 4, 10, 19, 59, 56, 248717, tzinfo=timezone.utc)


def test_parse_iso_datetime_handles_trailing_z() -> None:
    parsed = parse_iso_datetime(" 2026-04-11T17:24:46.344345Z ")

    assert parsed == datetime(2026, 4, 11, 17, 24, 46, 344345, tzinfo=timezone.utc)


def test_parse_iso_datetime_returns_aware_datetime_as_is() -> None:
    value = datetime(2026, 4, 11, 13, 24, 46, tzinfo=timezone(timedelta(hours=-4)))

    assert parse_iso_datetime(value) is value


def test_parse_iso_datetime_attaches_utc_to_naive_datetime() -> None:
    parsed = parse_iso_datetime(datetime(2026, 4, 11, 17, 24, 46))

    assert parsed == datetime(2026, 4, 11, 17, 24, 46, tzinfo=timezone.utc)


def test_parse_iso_datetime_returns_default_for_none() -> None:
    default = datetime(2026, 4, 11, 17, 24, 46, tzinfo=timezone.utc)

    assert parse_iso_datetime(None, default_none=default) is default


def test_parse_iso_datetime_raises_for_invalid_string() -> None:
    with pytest.raises(ValueError, match="Invalid ISO datetime string"):
        parse_iso_datetime("totally-not-a-timestamp")


def test_normalized_market_snapshot_from_dict_parses_nanosecond_timestamps() -> None:
    snapshot = NormalizedMarketSnapshot.from_dict(
        {
            "symbol": "AAPL",
            "asset_class": "equity",
            "quote_timestamp": "2026-04-11T17:24:46.344345659+00:00",
            "trade_timestamp": "2026-04-10T19:59:56.248717591+00:00",
            "source_timestamp": "2026-04-11T17:24:46.344345Z",
        }
    )

    assert snapshot.quote_timestamp == datetime(2026, 4, 11, 17, 24, 46, 344345, tzinfo=timezone.utc)
    assert snapshot.trade_timestamp == datetime(2026, 4, 10, 19, 59, 56, 248717, tzinfo=timezone.utc)
    assert snapshot.source_timestamp == datetime(2026, 4, 11, 17, 24, 46, 344345, tzinfo=timezone.utc)


def test_normalized_market_snapshot_from_dict_returns_none_for_invalid_timestamps() -> None:
    snapshot = NormalizedMarketSnapshot.from_dict(
        {
            "symbol": "AAPL",
            "asset_class": "equity",
            "quote_timestamp": "not-a-real-timestamp",
        }
    )

    assert snapshot.quote_timestamp is None
