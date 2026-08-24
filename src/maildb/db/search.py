"""Full-text search composition.

FTS5 handles the language part -- keywords, phrases, AND/OR/NOT, prefix
matching. Everything else is a structured filter applied in SQL alongside
it. Keeping the two separate means a user's search terms never have to be
escaped into a WHERE clause, and filters never have to be expressed in
FTS syntax.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from maildb.domain.errors import QueryRejected


@dataclass(frozen=True)
class SearchFilters:
    account_id: str | None = None
    mailbox: str | None = None
    sender: str | None = None
    recipient: str | None = None
    since: str | None = None
    until: str | None = None
    is_read: bool | None = None
    is_starred: bool | None = None
    has_attachments: bool | None = None
    #: Bulk mail is the majority of any real mailbox and is rarely what
    #: anyone is looking for, so it is excluded unless asked for.
    include_bulk: bool = False
    include_deleted: bool = False
    limit: int = 50


def _validate_match(query: str) -> str:
    """Reject an empty or bare-operator FTS query.

    FTS5 raises a bare ``OperationalError`` for malformed MATCH syntax,
    which surfaces to a caller as an opaque failure. Catching the common
    cases here produces something actionable instead.
    """
    stripped = query.strip()
    if not stripped:
        raise QueryRejected("empty search query", layer="search")
    if stripped.upper() in {"AND", "OR", "NOT", "NEAR"}:
        raise QueryRejected(
            f"{stripped!r} is an FTS operator, not a search term", layer="search"
        )
    return stripped


def build_search(query: str, filters: SearchFilters) -> tuple[str, list[Any]]:
    """Compose an FTS MATCH with structured filters.

    Returns ``(sql, params)``. Every filter is parameterized; only the
    MATCH expression comes from the user, and it goes into FTS5's own
    query language rather than into SQL syntax.
    """
    match = _validate_match(query)

    where: list[str] = ["message_fts MATCH ?"]
    params: list[Any] = [match]

    if not filters.include_deleted:
        where.append("m.deleted_at IS NULL")
    if not filters.include_bulk:
        where.append("m.list_id IS NULL")
        where.append("(m.auto_submitted IS NULL OR m.auto_submitted = 'no')")

    if filters.account_id:
        where.append("m.account_id = ?")
        params.append(filters.account_id)
    if filters.sender:
        where.append("m.sender_email LIKE ?")
        params.append(f"%{filters.sender.lower()}%")
    if filters.recipient:
        where.append("m.recipients LIKE ?")
        params.append(f"%{filters.recipient.lower()}%")
    if filters.since:
        where.append("m.received_at >= ?")
        params.append(filters.since)
    if filters.until:
        where.append("m.received_at <= ?")
        params.append(filters.until)
    if filters.is_read is not None:
        where.append("m.is_read = ?")
        params.append(int(filters.is_read))
    if filters.is_starred is not None:
        where.append("m.is_starred = ?")
        params.append(int(filters.is_starred))
    if filters.has_attachments is not None:
        where.append("m.has_attachments = ?")
        params.append(int(filters.has_attachments))
    if filters.mailbox:
        where.append(
            "EXISTS (SELECT 1 FROM message_labels ml "
            "JOIN mailboxes mb ON mb.id = ml.mailbox_id "
            "WHERE ml.message_id = m.id AND mb.name = ?)"
        )
        params.append(filters.mailbox)

    sql = f"""
        SELECT
            m.id,
            m.account_id,
            m.subject,
            m.sender_name,
            m.sender_email,
            m.received_at,
            m.is_read,
            m.has_attachments,
            m.snippet
        FROM message_fts
        JOIN messages m ON m.seq = message_fts.rowid
        WHERE {" AND ".join(where)}
        ORDER BY bm25(message_fts), m.received_at DESC
        LIMIT ?
    """
    params.append(filters.limit)
    return sql, params
