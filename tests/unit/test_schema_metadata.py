"""Schema metadata coverage.

Metadata drift is silent and expensive: a column with no description is a
column an agent will guess about. These tests make drift a build failure.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maildb.db.connection import READABLE_TABLES
from maildb.db.migrations import apply_migrations
from maildb.db.schema import (
    conventions,
    describe_table,
    documented_columns,
    list_tables,
)
from maildb.domain.errors import MaildbError
from tests.fixtures.corpus import Corpus


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "mail.db"
    apply_migrations(path)
    return path


def _live_columns(path: Path) -> dict[str, set[str]]:
    conn = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if row[0] in READABLE_TABLES
        ]
        return {
            table: {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for table in tables
        }
    finally:
        conn.close()


def test_every_column_is_documented(migrated: Path) -> None:
    documented = documented_columns()
    missing: list[str] = []
    for table, columns in _live_columns(migrated).items():
        for column in columns:
            if column not in documented.get(table, set()):
                missing.append(f"{table}.{column}")
    assert not missing, f"undocumented columns: {sorted(missing)}"


def test_metadata_documents_no_phantom_columns(migrated: Path) -> None:
    """The reverse drift: a description for a column that no longer exists."""
    live = _live_columns(migrated)
    phantom: list[str] = []
    for table, columns in documented_columns().items():
        if table == "message_fts":
            continue  # virtual table; columns are not in table_info
        for column in columns:
            if table in live and column not in live[table]:
                phantom.append(f"{table}.{column}")
    assert not phantom, f"documented but absent: {sorted(phantom)}"


def test_every_readable_table_is_described(migrated: Path) -> None:
    described = {t.name for t in list_tables(migrated)}
    assert described == set(READABLE_TABLES)


def test_audit_log_is_not_listed(migrated: Path) -> None:
    """A read-only agent should not be told about the write ledger."""
    assert "audit_log" not in {t.name for t in list_tables(migrated)}


def test_describe_refuses_a_non_readable_table(migrated: Path) -> None:
    with pytest.raises(MaildbError):
        describe_table(migrated, "audit_log")


def test_conventions_cover_the_things_that_bite() -> None:
    keys = conventions()
    assert {"timestamps", "soft_deletes", "json_columns", "identifiers"} <= set(keys)


def test_capability_governed_columns_name_their_gaps(corpus: Corpus) -> None:
    """The mechanism that stops the superset schema from lying.

    ``is_important`` is a Gmail concept. For the Apple Mail account it is
    always NULL, and an agent filtering on it would get an empty result
    that looks like an answer. describe_table must say so.
    """
    messages = describe_table(corpus.path, "messages")
    important = next(c for c in messages.columns if c.name == "is_important")
    assert important.capability == "importance_flag"
    assert important.null_for == ("apple_mail",)


def test_universal_columns_carry_no_false_caveat(corpus: Corpus) -> None:
    messages = describe_table(corpus.path, "messages")
    subject = next(c for c in messages.columns if c.name == "subject")
    assert subject.capability is None
    assert subject.null_for == ()


def test_described_columns_have_real_types(corpus: Corpus) -> None:
    messages = describe_table(corpus.path, "messages")
    seq = next(c for c in messages.columns if c.name == "seq")
    assert seq.sql_type == "INTEGER"
    assert all(c.description for c in messages.columns)
