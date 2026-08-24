"""The security suite.

**Every test in this module runs with the sqlglot parser disabled.**

That is the point. The parser produces good error messages; the
authorizer and the read-only connection are what actually protect the
database. A rejection that only happens because a string was refused
before execution proves nothing about what would happen if the string
got through (ADR 0002).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maildb.config import QueryLimits
from maildb.db import connection
from maildb.domain.errors import QueryRejected
from maildb.service.query import QueryService

#: Statements that must not execute, whatever they look like.
FORBIDDEN = [
    pytest.param("DROP TABLE messages", id="drop"),
    pytest.param("dRoP TaBlE messages", id="drop-mixed-case"),
    pytest.param("DELETE FROM messages", id="delete"),
    pytest.param("UPDATE messages SET subject = 'x'", id="update"),
    pytest.param(
        "INSERT INTO messages (id, account_id, provider_message_id, "
        "created_at, updated_at) VALUES ('x','y','z','2026-01-01T00:00:00Z',"
        "'2026-01-01T00:00:00Z')",
        id="insert",
    ),
    pytest.param("ALTER TABLE messages ADD COLUMN evil TEXT", id="alter"),
    pytest.param("CREATE TABLE evil (x TEXT)", id="create"),
    pytest.param("CREATE INDEX evil ON messages(subject)", id="create-index"),
    pytest.param("CREATE VIRTUAL TABLE evil USING fts5(x)", id="create-vtable"),
    pytest.param(
        "CREATE TRIGGER t AFTER INSERT ON messages BEGIN SELECT 1; END",
        id="create-trigger",
    ),
    pytest.param("ATTACH DATABASE '/tmp/evil.db' AS evil", id="attach"),
    pytest.param("DETACH DATABASE main", id="detach"),
    pytest.param("PRAGMA table_info(messages)", id="pragma-read"),
    pytest.param("PRAGMA journal_mode = DELETE", id="pragma-write"),
    pytest.param("VACUUM", id="vacuum"),
    pytest.param("REINDEX", id="reindex"),
    pytest.param("ANALYZE", id="analyze"),
    pytest.param("SELECT load_extension('/tmp/evil.dylib')", id="load-extension"),
    pytest.param("SELECT * FROM sqlite_master", id="sqlite-master"),
    pytest.param("SELECT * FROM audit_log", id="audit-log"),
    pytest.param(
        "SELECT * FROM messages /* sneak */ UNION ALL SELECT * FROM audit_log",
        id="audit-log-via-union",
    ),
    pytest.param(
        "WITH x AS (SELECT * FROM audit_log) SELECT * FROM x",
        id="audit-log-via-cte",
    ),
    pytest.param("SELECT * FROM messages; DROP TABLE messages", id="stacked"),
    pytest.param(
        "SELECT * FROM messages;\n-- comment\nDELETE FROM messages",
        id="stacked-with-comment",
    ),
]


@pytest.mark.parametrize("sql", FORBIDDEN)
def test_rejected_without_the_parser(unparsed_service: QueryService, sql: str) -> None:
    """The authorizer alone refuses every forbidden statement."""
    assert unparsed_service.limits.parser_enabled is False
    with pytest.raises(QueryRejected) as exc:
        unparsed_service.execute(sql)
    # Rejected by SQLite or its authorizer -- never merely by inspecting
    # the string, which is not enabled here at all.
    assert exc.value.layer in {"authorizer", "sqlite"}


@pytest.mark.parametrize("sql", FORBIDDEN)
def test_rejected_with_the_parser_too(service: QueryService, sql: str) -> None:
    """With the full stack, the same statements are refused earlier."""
    with pytest.raises(QueryRejected):
        service.execute(sql)


def test_forbidden_statements_leave_no_trace(unparsed_service: QueryService) -> None:
    """A refused write changes nothing, even in the presence of retries."""
    before = unparsed_service.execute("SELECT count(*) FROM messages").rows[0][0]
    for sql in ("DELETE FROM messages", "DROP TABLE messages"):
        with pytest.raises(QueryRejected):
            unparsed_service.execute(sql)
    after = unparsed_service.execute("SELECT count(*) FROM messages").rows[0][0]
    assert before == after


def test_read_only_connection_blocks_writes_without_the_authorizer(
    corpus: object, db_path: Path
) -> None:
    """Layer 1 in isolation.

    Strip the authorizer entirely and the ``mode=ro`` connection still
    refuses to write, because SQLite enforces that below any SQL. This is
    the floor the other layers sit on.
    """
    conn = connection.read_only(db_path)
    conn.set_authorizer(None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM messages")
    finally:
        conn.close()


def test_allowed_reads_still_work(unparsed_service: QueryService) -> None:
    """Denial must not be achieved by refusing everything."""
    result = unparsed_service.execute(
        "SELECT subject FROM messages WHERE deleted_at IS NULL ORDER BY received_at"
    )
    assert result.row_count == 4


def test_error_message_names_the_readable_tables(service: QueryService) -> None:
    """A denial should tell an agent what it *can* do."""
    with pytest.raises(QueryRejected) as exc:
        QueryService(service.db_path, QueryLimits(parser_enabled=False)).execute(
            "SELECT * FROM audit_log"
        )
    assert "messages" in str(exc.value)
