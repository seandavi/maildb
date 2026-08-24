"""Stable public identifiers.

Provider identifiers are never the public identity of a row: they are
provider-shaped, sometimes reused, and meaningless outside their own
account. Public identifiers are ULIDs, which sort by creation time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

from ulid import ULID

from maildb.domain.errors import MaildbError

#: Fixed-width ISO-8601 UTC. Lexicographic order == chronological order,
#: which is what makes plain TEXT comparison correct in SQL (ADR 0006).
TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"

_TIMESTAMP_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_URI_RE: Final = re.compile(r"^(?P<kind>[a-z]+)://(?P<account>[^/]+)/(?P<id>.+)$")


def new_id() -> str:
    """Mint a public identifier."""
    return str(ULID())


def now() -> str:
    """Current time in the canonical storage format."""
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


def to_timestamp(value: datetime) -> str:
    """Normalize a datetime to the canonical storage format.

    Naive datetimes are assumed UTC; aware ones are converted. Sub-second
    precision is discarded so every stored value is the same width.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def from_timestamp(value: str) -> datetime:
    """Parse a canonical timestamp back to an aware datetime."""
    if not is_timestamp(value):
        raise MaildbError(f"not a canonical timestamp: {value!r}")
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def is_timestamp(value: str) -> bool:
    """Whether a string is a canonical timestamp."""
    return bool(_TIMESTAMP_RE.match(value))


def message_uri(account_id: str, message_id: str) -> str:
    return f"email://{account_id}/{message_id}"


def thread_uri(account_id: str, thread_id: str) -> str:
    return f"thread://{account_id}/{thread_id}"


def attachment_uri(account_id: str, attachment_id: str) -> str:
    return f"attachment://{account_id}/{attachment_id}"


def parse_uri(uri: str) -> tuple[str, str, str]:
    """Split a maildb URI into ``(kind, account_id, id)``."""
    match = _URI_RE.match(uri)
    if match is None:
        raise MaildbError(f"malformed maildb URI: {uri!r}")
    return match["kind"], match["account"], match["id"]
