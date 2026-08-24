"""Sync engine behaviour.

The three properties that matter more than throughput: idempotent,
restartable, and non-destructive.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maildb.db import connection
from maildb.db.migrations import apply_migrations
from maildb.providers.base import (
    NormalizedAddress,
    NormalizedAttachment,
    NormalizedMailbox,
    NormalizedMessage,
    SyncCursor,
    SyncResult,
)
from maildb.sync.engine import SyncEngine
from tests.fixtures.fake_provider import APPLE_LIKE, FakeProvider


@pytest.fixture
def engine(tmp_path: Path) -> SyncEngine:
    path = tmp_path / "mail.db"
    apply_migrations(path)
    engine = SyncEngine(path)
    engine.ensure_account("test", "gmail", "sean@example.com")
    return engine


def _mailboxes() -> list[NormalizedMailbox]:
    return [
        NormalizedMailbox("INBOX", "INBOX", "inbox"),
        NormalizedMailbox("IMPORTANT", "IMPORTANT", "important"),
        NormalizedMailbox("Label_1", "Grants", "user"),
    ]


def _message(
    provider_id: str,
    *,
    subject: str = "Hello",
    thread: str | None = "t-1",
    labels: list[str] | None = None,
    received: str = "2026-08-20T09:00:00Z",
    **kwargs: object,
) -> NormalizedMessage:
    return NormalizedMessage(
        provider_message_id=provider_id,
        provider_thread_id=thread,
        subject=subject,
        sender=NormalizedAddress("Casey", "casey@example.org"),
        recipients=[NormalizedAddress(None, "sean@example.com")],
        received_at=received,
        internal_date=received,
        body_text=f"Body of {provider_id}",
        mailbox_ids=labels if labels is not None else ["INBOX"],
        **{
            "headers": [("Subject", subject), ("From", "casey@example.org")],
            **kwargs,
        },  # type: ignore[arg-type]
    )


def _count(path: Path, sql: str, *params: object) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def test_first_sync_inserts(engine: SyncEngine) -> None:
    provider = FakeProvider(
        results=[
            SyncResult(
                messages=[_message("m1"), _message("m2")],
                mailboxes=_mailboxes(),
                cursor=SyncCursor(token="100"),
            )
        ]
    )
    stats = engine.sync_account(provider, "test")
    assert (stats.added, stats.updated, stats.seen) == (2, 0, 2)
    assert _count(engine.db_path, "SELECT count(*) FROM messages") == 2
    assert _count(engine.db_path, "SELECT count(*) FROM mailboxes") == 3


def test_resync_is_idempotent(engine: SyncEngine) -> None:
    """The property that makes an interrupted sync safe to retry."""
    result = SyncResult(
        messages=[_message("m1"), _message("m2")],
        mailboxes=_mailboxes(),
        cursor=SyncCursor(token="100"),
    )
    engine.apply("test", result)
    before = _snapshot(engine.db_path)

    stats = engine.apply("test", result)
    assert stats.added == 0
    assert stats.updated == 2
    assert _snapshot(engine.db_path) == before


def _snapshot(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return list(
            conn.execute(
                "SELECT provider_message_id, subject, body_text, is_read, "
                "received_at FROM messages ORDER BY provider_message_id"
            )
        )
    finally:
        conn.close()


def test_cursor_is_stored_and_replayed(engine: SyncEngine) -> None:
    provider = FakeProvider(
        results=[
            SyncResult(mailboxes=_mailboxes(), cursor=SyncCursor(token="100")),
            SyncResult(mailboxes=_mailboxes(), cursor=SyncCursor(token="200")),
        ]
    )
    engine.sync_account(provider, "test")
    engine.sync_account(provider, "test")

    assert provider.seen_cursors[0] is None
    assert provider.seen_cursors[1] is not None
    assert provider.seen_cursors[1].token == "100"
    cursor = engine.current_cursor("test")
    assert cursor is not None and cursor.token == "200"


def test_failed_sync_leaves_the_index_and_cursor_intact(engine: SyncEngine) -> None:
    """A failed sync must not corrupt what a successful one built."""
    good = SyncResult(
        messages=[_message("m1")], mailboxes=_mailboxes(), cursor=SyncCursor("100")
    )
    engine.apply("test", good)
    before = _snapshot(engine.db_path)

    exploding = SyncResult(
        messages=[_message("m2"), _message("bad", received="not-a-timestamp")],
        mailboxes=_mailboxes(),
        cursor=SyncCursor("200"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        engine.apply("test", exploding)

    # The good message survives, the partial batch is gone entirely, and
    # the cursor still points at the last success.
    assert _snapshot(engine.db_path) == before
    cursor = engine.current_cursor("test")
    assert cursor is not None and cursor.token == "100"

    conn = sqlite3.connect(engine.db_path)
    try:
        status, error = conn.execute(
            "SELECT status, error FROM sync_state WHERE account_id = 'test'"
        ).fetchone()
    finally:
        conn.close()
    assert status == "failed"
    assert error


def test_failed_sync_leaves_fts_consistent(engine: SyncEngine) -> None:
    engine.apply(
        "test",
        SyncResult(messages=[_message("m1")], mailboxes=_mailboxes()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        engine.apply(
            "test",
            SyncResult(
                messages=[_message("m2"), _message("bad", received="nope")],
                mailboxes=_mailboxes(),
            ),
        )
    conn = connection.writable(engine.db_path)
    try:
        conn.execute("INSERT INTO message_fts(message_fts) VALUES ('integrity-check')")
        indexed = conn.execute("SELECT count(*) FROM message_fts").fetchone()[0]
        stored = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    assert indexed == stored == 1


def test_explicit_deletions_are_soft(engine: SyncEngine) -> None:
    engine.apply(
        "test",
        SyncResult(messages=[_message("m1"), _message("m2")], mailboxes=_mailboxes()),
    )
    stats = engine.apply(
        "test",
        SyncResult(mailboxes=_mailboxes(), deleted_provider_ids=["m1"]),
    )
    assert stats.deleted == 1
    # The row is still there -- a reconciliation bug costs staleness, not mail.
    assert _count(engine.db_path, "SELECT count(*) FROM messages") == 2
    assert (
        _count(
            engine.db_path,
            "SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL",
        )
        == 1
    )


def test_deleting_twice_counts_once(engine: SyncEngine) -> None:
    engine.apply("test", SyncResult(messages=[_message("m1")], mailboxes=_mailboxes()))
    first = engine.apply(
        "test", SyncResult(mailboxes=_mailboxes(), deleted_provider_ids=["m1"])
    )
    second = engine.apply(
        "test", SyncResult(mailboxes=_mailboxes(), deleted_provider_ids=["m1"])
    )
    assert (first.deleted, second.deleted) == (1, 0)


def test_a_reappearing_message_is_undeleted(engine: SyncEngine) -> None:
    """Absence in one pass must not permanently bury a message."""
    engine.apply("test", SyncResult(messages=[_message("m1")], mailboxes=_mailboxes()))
    engine.apply(
        "test", SyncResult(mailboxes=_mailboxes(), deleted_provider_ids=["m1"])
    )
    engine.apply("test", SyncResult(messages=[_message("m1")], mailboxes=_mailboxes()))
    assert (
        _count(
            engine.db_path,
            "SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL",
        )
        == 0
    )


def test_full_sync_treats_absence_as_deletion(engine: SyncEngine) -> None:
    """Only a full listing is authoritative about what exists."""
    engine.apply(
        "test",
        SyncResult(messages=[_message("m1"), _message("m2")], mailboxes=_mailboxes()),
    )
    engine.apply(
        "test",
        SyncResult(
            messages=[_message("m1")], mailboxes=_mailboxes(), was_full_sync=True
        ),
    )
    conn = sqlite3.connect(engine.db_path)
    try:
        gone = [
            r[0]
            for r in conn.execute(
                "SELECT provider_message_id FROM messages WHERE deleted_at IS NOT NULL"
            )
        ]
    finally:
        conn.close()
    assert gone == ["m2"]


def test_incremental_sync_does_not_treat_absence_as_deletion(
    engine: SyncEngine,
) -> None:
    """The mirror of the previous test, and the more dangerous direction."""
    engine.apply(
        "test",
        SyncResult(messages=[_message("m1"), _message("m2")], mailboxes=_mailboxes()),
    )
    engine.apply(
        "test",
        SyncResult(
            messages=[_message("m1")], mailboxes=_mailboxes(), was_full_sync=False
        ),
    )
    assert (
        _count(
            engine.db_path,
            "SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL",
        )
        == 0
    )


def test_many_to_many_labels(engine: SyncEngine) -> None:
    engine.apply(
        "test",
        SyncResult(
            messages=[_message("m1", labels=["INBOX", "IMPORTANT", "Label_1"])],
            mailboxes=_mailboxes(),
        ),
    )
    assert _count(engine.db_path, "SELECT count(*) FROM message_labels") == 3
    # Label-based providers leave the canonical folder NULL (ADR 0003).
    assert (
        _count(engine.db_path, "SELECT count(*) FROM messages WHERE mailbox_id IS NULL")
        == 1
    )


def test_single_folder_provider_sets_the_canonical_mailbox(
    engine: SyncEngine,
) -> None:
    engine.apply(
        "test",
        SyncResult(messages=[_message("m1", labels=["INBOX"])], mailboxes=_mailboxes()),
    )
    assert (
        _count(
            engine.db_path,
            "SELECT count(*) FROM messages WHERE mailbox_id IS NOT NULL",
        )
        == 1
    )
    # And the junction table is populated either way, so consumers can
    # join through it unconditionally.
    assert _count(engine.db_path, "SELECT count(*) FROM message_labels") == 1


def test_label_changes_replace_rather_than_accumulate(engine: SyncEngine) -> None:
    engine.apply(
        "test",
        SyncResult(
            messages=[_message("m1", labels=["INBOX", "IMPORTANT"])],
            mailboxes=_mailboxes(),
        ),
    )
    engine.apply(
        "test",
        SyncResult(
            messages=[_message("m1", labels=["Label_1"])], mailboxes=_mailboxes()
        ),
    )
    assert _count(engine.db_path, "SELECT count(*) FROM message_labels") == 1


def test_headers_are_stored_with_duplicates_preserved(engine: SyncEngine) -> None:
    message = _message(
        "m1",
        headers=[
            ("Received", "from mx1"),
            ("Received", "from mx2"),
            ("Subject", "Hello"),
        ],
    )
    engine.apply("test", SyncResult(messages=[message], mailboxes=_mailboxes()))
    assert (
        _count(
            engine.db_path,
            "SELECT count(*) FROM message_headers WHERE name_lower = 'received'",
        )
        == 2
    )


def test_downloaded_attachments_survive_a_resync(engine: SyncEngine) -> None:
    """Re-syncing must not orphan bytes already fetched to disk."""
    message = _message(
        "m1",
        attachments=[
            NormalizedAttachment("att-1", "1", "agenda.pdf", "application/pdf", 100)
        ],
    )
    engine.apply("test", SyncResult(messages=[message], mailboxes=_mailboxes()))

    conn = connection.writable(engine.db_path)
    try:
        conn.execute(
            "UPDATE attachments SET downloaded = 1, local_path = '/tmp/a.pdf', "
            "content_hash = 'deadbeef'"
        )
    finally:
        conn.close()

    engine.apply("test", SyncResult(messages=[message], mailboxes=_mailboxes()))

    conn = sqlite3.connect(engine.db_path)
    try:
        row = conn.execute(
            "SELECT downloaded, local_path, content_hash FROM attachments"
        ).fetchone()
    finally:
        conn.close()
    assert row == (1, "/tmp/a.pdf", "deadbeef")


def test_provider_threads_are_used_directly(engine: SyncEngine) -> None:
    engine.apply(
        "test",
        SyncResult(
            messages=[
                _message("m1", thread="t-1"),
                _message("m2", thread="t-1"),
                _message("m3", thread="t-2"),
            ],
            mailboxes=_mailboxes(),
        ),
    )
    conn = sqlite3.connect(engine.db_path)
    try:
        rows = dict(
            conn.execute("SELECT provider_thread_id, message_count FROM threads")
        )
        sources = {r[0] for r in conn.execute("SELECT thread_source FROM threads")}
    finally:
        conn.close()
    assert rows == {"t-1": 2, "t-2": 1}
    assert sources == {"provider"}


def test_threads_are_reconstructed_when_the_provider_has_none(
    engine: SyncEngine,
) -> None:
    """Apple Mail's situation: RFC-822 headers are all there is."""
    root = NormalizedMessage(
        provider_message_id="a1",
        subject="Retreat planning",
        message_id_header="<1@cu.edu>",
        received_at="2026-08-20T09:00:00Z",
        mailbox_ids=["INBOX"],
    )
    reply = NormalizedMessage(
        provider_message_id="a2",
        subject="Re: Retreat planning",
        message_id_header="<2@cu.edu>",
        in_reply_to="<1@cu.edu>",
        references_header="<1@cu.edu>",
        received_at="2026-08-21T09:00:00Z",
        mailbox_ids=["INBOX"],
    )
    other = NormalizedMessage(
        provider_message_id="a3",
        subject="Something else",
        message_id_header="<9@cu.edu>",
        received_at="2026-08-22T09:00:00Z",
        mailbox_ids=["INBOX"],
    )
    provider = FakeProvider(caps=set(APPLE_LIKE))
    assert provider.capabilities() == APPLE_LIKE

    engine.apply(
        "test",
        SyncResult(messages=[root, reply, other], mailboxes=_mailboxes()),
    )
    conn = sqlite3.connect(engine.db_path)
    try:
        counts = sorted(r[0] for r in conn.execute("SELECT message_count FROM threads"))
        sources = {r[0] for r in conn.execute("SELECT thread_source FROM threads")}
        assigned = conn.execute(
            "SELECT count(*) FROM messages WHERE thread_id IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert counts == [1, 2]
    # And it says so, rather than passing itself off as provider truth.
    assert sources == {"reconstructed"}
    assert assigned == 3


def test_synced_messages_are_searchable(engine: SyncEngine) -> None:
    engine.apply(
        "test",
        SyncResult(
            messages=[_message("m1", subject="Xenium platform quote")],
            mailboxes=_mailboxes(),
        ),
    )
    conn = sqlite3.connect(engine.db_path)
    try:
        hits = conn.execute(
            "SELECT count(*) FROM message_fts WHERE message_fts MATCH 'Xenium'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert hits == 1
