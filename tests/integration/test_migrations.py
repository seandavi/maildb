"""Migrations, and the schema invariants that depend on them."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maildb.db.migrations import (
    applied_migrations,
    apply_migrations,
    rollback_migrations,
)
from maildb.ids import new_id, now


def test_migrations_apply_and_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "mail.db"
    first = apply_migrations(path)
    assert first
    assert apply_migrations(path) == []
    assert applied_migrations(path) == first


def test_migrations_roll_back_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "mail.db"
    apply_migrations(path)
    rolled = rollback_migrations(path)
    assert rolled

    conn = sqlite3.connect(path)
    try:
        remaining = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not row[0].startswith(("sqlite_", "_yoyo", "yoyo"))
        }
    finally:
        conn.close()
    assert remaining == set()


def test_capabilities_are_seeded(tmp_path: Path) -> None:
    path = tmp_path / "mail.db"
    apply_migrations(path)
    conn = sqlite3.connect(path)
    try:
        rows = dict(
            conn.execute(
                "SELECT provider, supported FROM provider_capabilities "
                "WHERE capability = 'incremental_history'"
            )
        )
    finally:
        conn.close()
    # The asymmetry that shapes the whole sync design.
    assert rows == {"gmail": 1, "apple_mail": 0}


def test_messages_has_a_stable_integer_rowid(tmp_path: Path) -> None:
    """ADR 0001: an implicit rowid can be renumbered by VACUUM."""
    path = tmp_path / "mail.db"
    apply_migrations(path)
    conn = sqlite3.connect(path)
    try:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(messages)")}
        assert columns["seq"][2] == "INTEGER"
        assert columns["seq"][5] == 1  # primary key
    finally:
        conn.close()


@pytest.mark.parametrize(
    "value",
    ["2026-08-24 12:00:00", "2026-08-24T12:00:00", "2026-08-24t12:00:00z", "nonsense"],
)
def test_non_canonical_timestamps_are_rejected_by_the_database(
    tmp_path: Path, value: str
) -> None:
    """The convention is enforced in the schema, not merely documented."""
    path = tmp_path / "mail.db"
    apply_migrations(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO accounts (id, provider, email, created_at, updated_at) "
            "VALUES ('a', 'gmail', 'x@example.com', ?, ?)",
            (now(), now()),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages (id, account_id, provider_message_id, "
                "received_at, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (new_id(), "a", "pm-1", value, now(), now()),
            )
    finally:
        conn.close()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    from maildb.db import connection

    path = tmp_path / "mail.db"
    apply_migrations(path)
    conn = connection.writable(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages (id, account_id, provider_message_id, "
                "created_at, updated_at) VALUES (?,?,?,?,?)",
                (new_id(), "no-such-account", "pm-1", now(), now()),
            )
    finally:
        conn.close()
