"""Full-text index consistency.

The index is maintained by triggers rather than application code, so
these tests exercise the triggers directly: whatever the write path does,
FTS must agree with ``messages`` afterwards.
"""

from __future__ import annotations

from pathlib import Path

from maildb.db import connection
from maildb.ids import new_id, now
from tests.fixtures.corpus import Corpus


def _fts_subjects(path: Path) -> set[str]:
    conn = connection.writable(path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT m.subject FROM message_fts "
                "JOIN messages m ON m.seq = message_fts.rowid"
            )
        }
    finally:
        conn.close()


def _table_subjects(path: Path) -> set[str]:
    conn = connection.writable(path)
    try:
        return {row[0] for row in conn.execute("SELECT subject FROM messages")}
    finally:
        conn.close()


def test_insert_is_immediately_searchable(corpus: Corpus) -> None:
    conn = connection.writable(corpus.path)
    try:
        conn.execute(
            "INSERT INTO messages (id, account_id, provider_message_id, subject, "
            "body_text, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                new_id(),
                corpus.gmail_account,
                "pm-new",
                "Xenium platform quote",
                "Attaching the quote for the Xenium instrument.",
                now(),
                now(),
            ),
        )
        found = conn.execute(
            "SELECT count(*) FROM message_fts WHERE message_fts MATCH 'Xenium'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert found == 1


def test_update_reindexes(corpus: Corpus) -> None:
    conn = connection.writable(corpus.path)
    try:
        conn.execute(
            "UPDATE messages SET body_text = 'completely different content' "
            "WHERE provider_message_id = 'pm-grant'"
        )
        # "October" appears only in the body that was just replaced.
        stale = conn.execute(
            "SELECT count(*) FROM message_fts WHERE message_fts MATCH 'October'"
        ).fetchone()[0]
        fresh = conn.execute(
            "SELECT count(*) FROM message_fts WHERE message_fts MATCH 'completely'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stale == 0
    assert fresh == 1


def test_delete_removes_from_the_index(corpus: Corpus) -> None:
    conn = connection.writable(corpus.path)
    try:
        conn.execute("DELETE FROM messages WHERE provider_message_id = 'pm-newsletter'")
    finally:
        conn.close()
    assert _fts_subjects(corpus.path) == _table_subjects(corpus.path)


def test_index_survives_a_churn_of_mixed_operations(corpus: Corpus) -> None:
    """Deterministic churn: the index must track the table through all of it."""
    conn = connection.writable(corpus.path)
    try:
        ids = []
        for i in range(40):
            message_id = new_id()
            ids.append(message_id)
            conn.execute(
                "INSERT INTO messages (id, account_id, provider_message_id, subject, "
                "body_text, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    message_id,
                    corpus.gmail_account,
                    f"pm-churn-{i}",
                    f"Subject number {i}",
                    f"Body text {i} about grants",
                    now(),
                    now(),
                ),
            )
        for i, message_id in enumerate(ids):
            if i % 3 == 0:
                conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            elif i % 3 == 1:
                conn.execute(
                    "UPDATE messages SET subject = ? WHERE id = ?",
                    (f"Revised subject {i}", message_id),
                )
    finally:
        conn.close()

    assert _fts_subjects(corpus.path) == _table_subjects(corpus.path)


def test_integrity_check_passes(corpus: Corpus) -> None:
    """FTS5's own consistency check, which compares index to content."""
    conn = connection.writable(corpus.path)
    try:
        conn.execute("INSERT INTO message_fts(message_fts) VALUES ('integrity-check')")
    finally:
        conn.close()


def test_html_bodies_are_not_indexed(corpus: Corpus) -> None:
    """body_html is excluded from FTS on purpose (ADR 0008)."""
    conn = connection.writable(corpus.path)
    try:
        conn.execute(
            "UPDATE messages SET body_html = '<p>unindexedmagicword</p>' "
            "WHERE provider_message_id = 'pm-grant'"
        )
        hits = conn.execute(
            "SELECT count(*) FROM message_fts "
            "WHERE message_fts MATCH 'unindexedmagicword'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert hits == 0
