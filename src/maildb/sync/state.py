"""Sync bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from maildb.domain.enums import SyncStatus
from maildb.ids import now
from maildb.providers.base import SyncCursor


@dataclass
class SyncStats:
    """What one sync pass did. Reported, logged, and stored."""

    seen: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    mailboxes: int = 0
    errors: list[str] = field(default_factory=list)

    def __add__(self, other: SyncStats) -> SyncStats:
        return SyncStats(
            seen=self.seen + other.seen,
            added=self.added + other.added,
            updated=self.updated + other.updated,
            deleted=self.deleted + other.deleted,
            mailboxes=self.mailboxes + other.mailboxes,
            errors=[*self.errors, *other.errors],
        )


def load_cursor(conn: sqlite3.Connection, account_id: str) -> SyncCursor | None:
    row = conn.execute(
        "SELECT provider_cursor, mailbox_watermarks FROM sync_state "
        "WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    token, watermarks = row[0], row[1]
    if not token and not watermarks:
        return None
    return SyncCursor(
        token=token,
        watermarks=json.loads(watermarks) if watermarks else {},
    )


def mark_started(conn: sqlite3.Connection, account_id: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (account_id, last_sync_started, status)
        VALUES (?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            last_sync_started = excluded.last_sync_started,
            status = excluded.status,
            error = NULL
        """,
        (account_id, now(), SyncStatus.RUNNING.value),
    )


def mark_completed(
    conn: sqlite3.Connection,
    account_id: str,
    cursor: SyncCursor,
    stats: SyncStats,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (
            account_id, provider_cursor, mailbox_watermarks,
            last_sync_completed, messages_seen, messages_added,
            messages_updated, messages_deleted, status, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(account_id) DO UPDATE SET
            provider_cursor = excluded.provider_cursor,
            mailbox_watermarks = excluded.mailbox_watermarks,
            last_sync_completed = excluded.last_sync_completed,
            messages_seen = excluded.messages_seen,
            messages_added = excluded.messages_added,
            messages_updated = excluded.messages_updated,
            messages_deleted = excluded.messages_deleted,
            status = excluded.status,
            error = NULL
        """,
        (
            account_id,
            cursor.token,
            json.dumps(cursor.watermarks) if cursor.watermarks else None,
            now(),
            stats.seen,
            stats.added,
            stats.updated,
            stats.deleted,
            SyncStatus.OK.value,
        ),
    )


def mark_failed(conn: sqlite3.Connection, account_id: str, error: str) -> None:
    """Record a failure without disturbing the stored cursor.

    The cursor is deliberately untouched: a failed sync must be safe to
    retry from where the last successful one finished.
    """
    conn.execute(
        """
        INSERT INTO sync_state (account_id, status, error)
        VALUES (?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            status = excluded.status,
            error = excluded.error
        """,
        (account_id, SyncStatus.FAILED.value, error[:2000]),
    )


def mark_reconciled(conn: sqlite3.Connection, account_id: str) -> None:
    conn.execute(
        "UPDATE sync_state SET last_reconciled_at = ? WHERE account_id = ?",
        (now(), account_id),
    )
