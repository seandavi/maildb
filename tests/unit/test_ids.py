"""Identifiers and the timestamp convention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from maildb.domain.errors import MaildbError
from maildb.ids import (
    from_timestamp,
    is_timestamp,
    message_uri,
    new_id,
    now,
    parse_uri,
    to_timestamp,
)


def test_ids_are_unique_and_sortable() -> None:
    ids = [new_id() for _ in range(100)]
    assert len(set(ids)) == 100
    assert ids == sorted(ids)  # ULIDs sort by creation time


def test_now_is_canonical() -> None:
    assert is_timestamp(now())


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-24T12:00:00Z",
        "1999-01-01T00:00:00Z",
    ],
)
def test_accepts_canonical_timestamps(value: str) -> None:
    assert is_timestamp(value)


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-24 12:00:00",  # no T, no Z
        "2026-08-24T12:00:00",  # no zone
        "2026-08-24T12:00:00+00:00",  # offset rather than Z
        "2026-08-24T12:00:00.123Z",  # sub-second precision breaks fixed width
        "2026-8-24T12:00:00Z",  # unpadded month
        "2026-08-24t12:00:00z",  # lowercase
    ],
)
def test_rejects_non_canonical_timestamps(value: str) -> None:
    """Anything that would break lexicographic ordering is not a timestamp."""
    assert not is_timestamp(value)


def test_offsets_are_converted_not_truncated() -> None:
    aware = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone(timedelta(hours=-6)))
    assert to_timestamp(aware) == "2026-08-24T18:00:00Z"


def test_naive_datetimes_are_assumed_utc() -> None:
    assert to_timestamp(datetime(2026, 8, 24, 12, 0, 0)) == "2026-08-24T12:00:00Z"


def test_subsecond_precision_is_discarded() -> None:
    stamped = to_timestamp(datetime(2026, 8, 24, 12, 0, 0, 987654, tzinfo=UTC))
    assert stamped == "2026-08-24T12:00:00Z"


def test_roundtrip() -> None:
    original = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
    assert from_timestamp(to_timestamp(original)) == original


def test_lexicographic_order_is_chronological() -> None:
    """The property the whole convention exists to guarantee."""
    stamps = [
        to_timestamp(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=d, hours=d))
        for d in range(0, 400, 7)
    ]
    assert sorted(stamps) == stamps


def test_uri_roundtrip() -> None:
    uri = message_uri("gmail-personal", "01J000")
    assert parse_uri(uri) == ("email", "gmail-personal", "01J000")


def test_malformed_uri_is_rejected() -> None:
    with pytest.raises(MaildbError):
        parse_uri("just-an-id")
