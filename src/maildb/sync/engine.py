"""The sync engine.

Provider-agnostic by construction: it consumes
:class:`~maildb.providers.base.SyncResult` and knows nothing about Gmail
or AppleScript.

Three properties matter more than speed:

*Idempotent.* Re-running a sync over the same input changes nothing.
Upserts key on ``(account_id, provider_message_id)``.

*Restartable.* Everything lands in one transaction per pass. An
interrupted sync leaves the previous state intact, cursor included, so
the retry resumes rather than double-writes.

*Non-destructive.* Deletions are soft. A reconciliation bug produces
stale rows, never lost mail.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import structlog

from maildb.db import connection
from maildb.domain.enums import ThreadSource
from maildb.ids import new_id, now
from maildb.providers.base import (
    EmailProvider,
    NormalizedAddress,
    NormalizedMailbox,
    NormalizedMessage,
    SyncCursor,
    SyncResult,
)
from maildb.sync import state
from maildb.sync.state import SyncStats
from maildb.sync.threads import ThreadCandidate, group_threads, parse_message_ids

log = structlog.get_logger(__name__)


def _addresses(values: Iterable[NormalizedAddress]) -> str | None:
    emails = [a.email for a in values]
    return json.dumps(emails) if emails else None


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


class SyncEngine:
    """Applies provider output to the local index."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # -- accounts and mailboxes -----------------------------------------

    def ensure_account(
        self,
        account_id: str,
        provider: str,
        email: str,
        *,
        display_name: str | None = None,
        auth_method: str | None = None,
        credential_ref: str | None = None,
    ) -> None:
        conn = connection.writable(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO accounts (
                    id, provider, email, display_name, auth_method,
                    credential_ref, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    email = excluded.email,
                    display_name = excluded.display_name,
                    auth_method = excluded.auth_method,
                    credential_ref = excluded.credential_ref,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    provider,
                    email,
                    display_name,
                    auth_method,
                    credential_ref,
                    now(),
                    now(),
                ),
            )
        finally:
            conn.close()

    def _upsert_mailboxes(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        mailboxes: list[NormalizedMailbox],
    ) -> dict[str, str]:
        """Upsert mailboxes, returning provider id to internal id."""
        for mailbox in mailboxes:
            conn.execute(
                """
                INSERT INTO mailboxes (
                    id, account_id, provider_mailbox_id, name, mailbox_type,
                    label_type, message_list_visibility, label_list_visibility,
                    messages_total, messages_unread, threads_total,
                    threads_unread, color_background, color_text,
                    provider_metadata, updated_at
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id, provider_mailbox_id) DO UPDATE SET
                    name = excluded.name,
                    mailbox_type = excluded.mailbox_type,
                    label_type = excluded.label_type,
                    message_list_visibility = excluded.message_list_visibility,
                    label_list_visibility = excluded.label_list_visibility,
                    messages_total = excluded.messages_total,
                    messages_unread = excluded.messages_unread,
                    threads_total = excluded.threads_total,
                    threads_unread = excluded.threads_unread,
                    color_background = excluded.color_background,
                    color_text = excluded.color_text,
                    provider_metadata = excluded.provider_metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    new_id(),
                    account_id,
                    mailbox.provider_mailbox_id,
                    mailbox.name,
                    mailbox.mailbox_type,
                    mailbox.label_type,
                    mailbox.message_list_visibility,
                    mailbox.label_list_visibility,
                    mailbox.messages_total,
                    mailbox.messages_unread,
                    mailbox.threads_total,
                    mailbox.threads_unread,
                    mailbox.color_background,
                    mailbox.color_text,
                    json.dumps(mailbox.provider_metadata)
                    if mailbox.provider_metadata
                    else None,
                    now(),
                ),
            )
        return self._mailbox_index(conn, account_id)

    @staticmethod
    def _mailbox_index(conn: sqlite3.Connection, account_id: str) -> dict[str, str]:
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT provider_mailbox_id, id FROM mailboxes WHERE account_id = ?",
                (account_id,),
            )
        }

    # -- messages --------------------------------------------------------

    def _upsert_message(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        message: NormalizedMessage,
        mailbox_index: dict[str, str],
    ) -> tuple[str, bool]:
        """Insert or update one message. Returns ``(id, was_inserted)``."""
        existing = conn.execute(
            "SELECT id FROM messages WHERE account_id = ? AND provider_message_id = ?",
            (account_id, message.provider_message_id),
        ).fetchone()

        message_id = existing[0] if existing else new_id()
        inserted = existing is None

        # A single-folder provider names exactly one mailbox; a
        # label-based one names many and leaves this NULL (ADR 0003).
        canonical_mailbox = (
            mailbox_index.get(message.mailbox_ids[0])
            if len(message.mailbox_ids) == 1
            else None
        )

        values = (
            message.provider_thread_id,
            message.message_id_header,
            message.in_reply_to,
            message.references_header,
            canonical_mailbox,
            message.subject,
            message.sender.name if message.sender else None,
            message.sender.email if message.sender else None,
            _addresses(message.recipients),
            _addresses(message.cc),
            _addresses(message.bcc),
            _addresses(message.reply_to),
            message.sent_at,
            message.received_at,
            message.internal_date,
            message.body_text,
            message.body_html,
            message.snippet,
            int(bool(message.attachments)),
            message.history_id,
            message.size_estimate,
            _flag(message.is_read),
            _flag(message.is_starred),
            _flag(message.is_draft),
            _flag(message.is_sent),
            _flag(message.is_important),
            _flag(message.is_spam),
            _flag(message.is_trash),
            _flag(message.is_answered),
            message.list_id,
            message.list_unsubscribe,
            message.precedence,
            message.auto_submitted,
            json.dumps(message.provider_metadata)
            if message.provider_metadata
            else None,
            now(),
        )

        if inserted:
            conn.execute(
                """
                INSERT INTO messages (
                    id, account_id, provider_message_id,
                    provider_thread_id, message_id_header, in_reply_to,
                    references_header, mailbox_id, subject, sender_name,
                    sender_email, recipients, cc, bcc, reply_to, sent_at,
                    received_at, internal_date, body_text, body_html, snippet,
                    has_attachments, history_id, size_estimate, is_read,
                    is_starred, is_draft, is_sent, is_important, is_spam,
                    is_trash, is_answered, list_id, list_unsubscribe,
                    precedence, auto_submitted, provider_metadata,
                    updated_at, created_at
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                        ?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (message_id, account_id, message.provider_message_id, *values, now()),
            )
        else:
            conn.execute(
                """
                UPDATE messages SET
                    provider_thread_id = ?, message_id_header = ?,
                    in_reply_to = ?, references_header = ?, mailbox_id = ?,
                    subject = ?, sender_name = ?, sender_email = ?,
                    recipients = ?, cc = ?, bcc = ?, reply_to = ?,
                    sent_at = ?, received_at = ?, internal_date = ?,
                    body_text = ?, body_html = ?, snippet = ?,
                    has_attachments = ?, history_id = ?, size_estimate = ?,
                    is_read = ?, is_starred = ?, is_draft = ?, is_sent = ?,
                    is_important = ?, is_spam = ?, is_trash = ?,
                    is_answered = ?, list_id = ?, list_unsubscribe = ?,
                    precedence = ?, auto_submitted = ?, provider_metadata = ?,
                    updated_at = ?,
                    -- A message that reappears at the provider is not
                    -- deleted, whatever a previous sweep concluded.
                    deleted_at = NULL
                WHERE id = ?
                """,
                (*values, message_id),
            )

        self._replace_labels(conn, message_id, message.mailbox_ids, mailbox_index)
        self._replace_headers(conn, message_id, message.headers)
        self._replace_attachments(conn, message_id, message)
        return message_id, inserted

    @staticmethod
    def _replace_labels(
        conn: sqlite3.Connection,
        message_id: str,
        provider_mailbox_ids: list[str],
        mailbox_index: dict[str, str],
    ) -> None:
        conn.execute("DELETE FROM message_labels WHERE message_id = ?", (message_id,))
        for provider_mailbox_id in provider_mailbox_ids:
            internal = mailbox_index.get(provider_mailbox_id)
            if internal is None:
                # A label the provider reported on a message but not in
                # its label list. Skipping is correct; the next full sync
                # picks it up.
                continue
            conn.execute(
                "INSERT OR IGNORE INTO message_labels (message_id, mailbox_id) "
                "VALUES (?, ?)",
                (message_id, internal),
            )

    @staticmethod
    def _replace_headers(
        conn: sqlite3.Connection, message_id: str, headers: list[tuple[str, str]]
    ) -> None:
        conn.execute("DELETE FROM message_headers WHERE message_id = ?", (message_id,))
        for ordinal, (name, value) in enumerate(headers):
            conn.execute(
                "INSERT INTO message_headers "
                "(message_id, ordinal, name, name_lower, value) VALUES (?,?,?,?,?)",
                (message_id, ordinal, name, name.lower(), value),
            )

    @staticmethod
    def _replace_attachments(
        conn: sqlite3.Connection, message_id: str, message: NormalizedMessage
    ) -> None:
        """Refresh attachment metadata, preserving anything downloaded.

        Re-syncing a message must not orphan bytes already fetched to
        disk, so downloaded state is carried across by filename.
        """
        downloaded = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT filename, local_path, content_hash, downloaded "
                "FROM attachments WHERE message_id = ? AND downloaded = 1",
                (message_id,),
            )
        }
        conn.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))
        for attachment in message.attachments:
            local_path, content_hash, was_downloaded = downloaded.get(
                attachment.filename or "", (None, None, 0)
            )
            conn.execute(
                """
                INSERT INTO attachments (
                    id, message_id, provider_attachment_id, part_id,
                    content_id, is_inline, filename, mime_type, size_bytes,
                    content_hash, local_path, downloaded, created_at
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id(),
                    message_id,
                    attachment.provider_attachment_id,
                    attachment.part_id,
                    attachment.content_id,
                    int(attachment.is_inline),
                    attachment.filename,
                    attachment.mime_type,
                    attachment.size_bytes,
                    content_hash,
                    local_path,
                    was_downloaded,
                    now(),
                ),
            )

    # -- threads ---------------------------------------------------------

    def _assign_threads(self, conn: sqlite3.Connection, account_id: str) -> None:
        """Group messages into threads.

        Provider-supplied thread ids are used directly. Everything else is
        reconstructed from RFC-822 headers, and says so via
        ``threads.thread_source`` so consumers know what they are looking
        at.
        """
        for row in conn.execute(
            "SELECT DISTINCT provider_thread_id FROM messages "
            "WHERE account_id = ? AND provider_thread_id IS NOT NULL",
            (account_id,),
        ).fetchall():
            self._upsert_thread(
                conn,
                account_id,
                row[0],
                ThreadSource.PROVIDER,
                "provider_thread_id = ?",
                (row[0],),
            )

        unthreaded = conn.execute(
            "SELECT id, message_id_header, in_reply_to, references_header, subject "
            "FROM messages WHERE account_id = ? AND provider_thread_id IS NULL",
            (account_id,),
        ).fetchall()
        if not unthreaded:
            return

        candidates = [
            ThreadCandidate(
                key=row[0],
                message_id_header=row[1],
                references=parse_message_ids(row[2]) + parse_message_ids(row[3]),
                subject=row[4],
            )
            for row in unthreaded
        ]
        for root, members in group_threads(candidates).items():
            placeholders = ",".join("?" for _ in members)
            self._upsert_thread(
                conn,
                account_id,
                f"recon:{root}",
                ThreadSource.RECONSTRUCTED,
                f"id IN ({placeholders})",
                tuple(members),
            )

    @staticmethod
    def _upsert_thread(
        conn: sqlite3.Connection,
        account_id: str,
        provider_thread_id: str,
        source: ThreadSource,
        predicate: str,
        params: tuple[str, ...],
    ) -> None:
        summary = conn.execute(
            f"""
            SELECT min(received_at), max(received_at), count(*),
                   min(subject), max(snippet)
            FROM messages WHERE account_id = ? AND {predicate}
            """,
            (account_id, *params),
        ).fetchone()
        if summary is None or summary[2] == 0:
            return

        first, last, count, subject, snippet = summary
        participants = sorted(
            {
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT sender_email FROM messages "
                    f"WHERE account_id = ? AND {predicate} "
                    f"AND sender_email IS NOT NULL",
                    (account_id, *params),
                )
            }
        )

        conn.execute(
            """
            INSERT INTO threads (
                id, account_id, provider_thread_id, thread_source, subject,
                first_message_at, last_message_at, message_count,
                participants, snippet, updated_at
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, provider_thread_id) DO UPDATE SET
                thread_source = excluded.thread_source,
                subject = excluded.subject,
                first_message_at = excluded.first_message_at,
                last_message_at = excluded.last_message_at,
                message_count = excluded.message_count,
                participants = excluded.participants,
                snippet = excluded.snippet,
                updated_at = excluded.updated_at
            """,
            (
                new_id(),
                account_id,
                provider_thread_id,
                source.value,
                subject,
                first,
                last,
                count,
                json.dumps(participants),
                snippet,
                now(),
            ),
        )
        thread_id = conn.execute(
            "SELECT id FROM threads WHERE account_id = ? AND provider_thread_id = ?",
            (account_id, provider_thread_id),
        ).fetchone()[0]
        conn.execute(
            f"UPDATE messages SET thread_id = ? WHERE account_id = ? AND {predicate}",
            (thread_id, account_id, *params),
        )

    # -- the pass itself -------------------------------------------------

    def apply(self, account_id: str, result: SyncResult) -> SyncStats:
        """Apply one provider result, atomically.

        The whole pass is a single transaction: an interruption leaves
        the previous index and cursor untouched, so the retry resumes
        rather than double-writing.
        """
        stats = SyncStats()
        conn = connection.writable(self.db_path)
        try:
            conn.execute("BEGIN")
            mailbox_index = self._upsert_mailboxes(conn, account_id, result.mailboxes)
            stats.mailboxes = len(result.mailboxes)

            for message in result.messages:
                _, inserted = self._upsert_message(
                    conn, account_id, message, mailbox_index
                )
                stats.seen += 1
                if inserted:
                    stats.added += 1
                else:
                    stats.updated += 1

            stats.deleted = self._soft_delete(conn, account_id, result)
            self._assign_threads(conn, account_id)
            state.mark_completed(conn, account_id, result.cursor, stats)
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            # The cursor is untouched by a failure, so the retry resumes
            # from the last success rather than from the start.
            failure = connection.writable(self.db_path)
            try:
                state.mark_failed(failure, account_id, str(exc))
            finally:
                failure.close()
            log.error("sync.failed", account_id=account_id, error=str(exc))
            raise
        finally:
            conn.close()

        log.info(
            "sync.completed",
            account_id=account_id,
            seen=stats.seen,
            added=stats.added,
            updated=stats.updated,
            deleted=stats.deleted,
        )
        return stats

    def _soft_delete(
        self, conn: sqlite3.Connection, account_id: str, result: SyncResult
    ) -> int:
        """Mark messages deleted. Rows are never removed."""
        deleted = 0
        for provider_id in result.deleted_provider_ids:
            cursor = conn.execute(
                "UPDATE messages SET deleted_at = ?, updated_at = ? "
                "WHERE account_id = ? AND provider_message_id = ? "
                "AND deleted_at IS NULL",
                (now(), now(), account_id, provider_id),
            )
            deleted += cursor.rowcount

        if result.was_full_sync:
            # A full listing is authoritative about what exists, so
            # anything absent from it is gone. This is the only safe place
            # to infer deletion from absence.
            present = [m.provider_message_id for m in result.messages]
            placeholders = ",".join("?" for _ in present) or "NULL"
            cursor = conn.execute(
                f"""
                UPDATE messages SET deleted_at = ?, updated_at = ?
                WHERE account_id = ? AND deleted_at IS NULL
                  AND provider_message_id NOT IN ({placeholders})
                """,
                (now(), now(), account_id, *present),
            )
            deleted += cursor.rowcount
        return deleted

    def sync_account(self, provider: EmailProvider, account_id: str) -> SyncStats:
        """Run one sync pass for an account."""
        conn = connection.writable(self.db_path)
        try:
            cursor = state.load_cursor(conn, account_id)
            state.mark_started(conn, account_id)
        finally:
            conn.close()

        log.info("sync.started", account_id=account_id, incremental=cursor is not None)
        result = provider.sync(cursor)
        return self.apply(account_id, result)

    def current_cursor(self, account_id: str) -> SyncCursor | None:
        conn = connection.writable(self.db_path)
        try:
            return state.load_cursor(conn, account_id)
        finally:
            conn.close()
