"""MCP tool surface.

Tested as functions rather than through a protocol handshake: the value
is in what the tools return, not in the transport, and the SDK owns the
transport (ADR 0011).
"""

from __future__ import annotations

import json
from typing import Any

from maildb.config import Config, DatabaseConfig, QueryLimits
from maildb.mcp.server import INSTRUCTIONS, MaildbTools, build_server
from tests.fixtures.corpus import Corpus


def _tools(corpus: Corpus, **limits: Any) -> MaildbTools:
    return MaildbTools(
        Config(
            database=DatabaseConfig(path=corpus.path),
            query=QueryLimits(**limits) if limits else QueryLimits(),
        )
    )


def test_list_tables_includes_conventions(corpus: Corpus) -> None:
    """An agent needs the conventions before it writes its first query."""
    payload = _tools(corpus).list_tables()
    names = {t["name"] for t in payload["tables"]}
    assert "messages" in names
    assert "audit_log" not in names
    assert "timestamps" in payload["conventions"]


def test_describe_table_carries_provider_caveats(corpus: Corpus) -> None:
    """The mechanism that prevents a confident wrong answer.

    Without this, an agent asked about important mail on the Apple Mail
    account gets an empty result and reports "none" -- which is false.
    """
    payload = _tools(corpus).describe_table("messages")
    columns = {c["name"]: c for c in payload["columns"]}
    assert columns["is_important"]["always_null_for"] == ["apple_mail"]
    assert "always_null_for" not in columns["subject"]


def test_describe_unknown_table_suggests_alternatives(corpus: Corpus) -> None:
    payload = _tools(corpus).describe_table("nonexistent")
    assert "error" in payload
    assert "messages" in payload["available_tables"]


def test_describe_refuses_the_audit_log(corpus: Corpus) -> None:
    payload = _tools(corpus).describe_table("audit_log")
    assert "error" in payload


def test_execute_query_returns_rows(corpus: Corpus) -> None:
    payload = _tools(corpus).execute_query(
        "SELECT subject FROM messages WHERE deleted_at IS NULL"
    )
    assert payload["row_count"] == 4
    assert "error" not in payload


def test_rejections_come_back_as_readable_errors(corpus: Corpus) -> None:
    """A model that can read why it was refused can fix its own query."""
    payload = _tools(corpus).execute_query("DROP TABLE messages")
    assert "error" in payload
    assert "SELECT" in payload["error"]


def test_reading_the_audit_log_is_refused(corpus: Corpus) -> None:
    payload = _tools(corpus).execute_query("SELECT * FROM audit_log")
    assert "error" in payload


def test_truncation_is_announced_not_hidden(corpus: Corpus) -> None:
    """A truncated result presented as complete is a wrong answer."""
    payload = _tools(corpus, max_rows=2).execute_query("SELECT id FROM messages")
    assert payload["truncated"] is True
    assert "not present this as a complete result" in payload["note"]


def test_untruncated_results_carry_no_note(corpus: Corpus) -> None:
    payload = _tools(corpus).execute_query("SELECT id FROM messages")
    assert "note" not in payload


def test_the_question_an_agent_actually_gets_asked(corpus: Corpus) -> None:
    """M4's exit criterion, as a query.

    "What did Wells Messersmith send me that I have not replied to?" --
    answerable in one query from the schema alone, with no hints.
    """
    payload = _tools(corpus).execute_query(
        """
        SELECT m.subject, m.received_at
        FROM messages m
        WHERE m.sender_email LIKE '%wells%'
          AND m.deleted_at IS NULL
          AND m.list_id IS NULL
          AND (m.is_answered IS NULL OR m.is_answered = 0)
        ORDER BY m.received_at DESC
        """
    )
    assert payload["row_count"] == 1
    assert payload["rows"][0][0] == "DBMI faculty meeting agenda"


def test_capability_check_distinguishes_absent_from_unsupported(
    corpus: Corpus,
) -> None:
    """The second exit criterion: knowing when an empty result is a lie."""
    tools = _tools(corpus)
    empty = tools.execute_query(
        "SELECT count(*) FROM messages WHERE account_id = 'cu' AND is_important = 1"
    )
    assert empty["rows"][0][0] == 0

    # ...and the table that explains why that zero is not an answer.
    why = tools.execute_query(
        "SELECT supported, notes FROM provider_capabilities "
        "WHERE provider = 'apple_mail' AND capability = 'importance_flag'"
    )
    assert why["rows"][0][0] == 0
    assert "not exposed" in why["rows"][0][1]


def test_attachment_metadata(corpus: Corpus) -> None:
    tools = _tools(corpus)
    payload = tools.get_attachment(corpus.message_ids["faculty"], "agenda.pdf")
    assert payload["mime_type"] == "application/pdf"
    assert payload["downloaded"] == 0


def test_missing_attachment_is_an_error_not_an_empty_dict(corpus: Corpus) -> None:
    payload = _tools(corpus).get_attachment("nope", "nope.pdf")
    assert "error" in payload


def test_instructions_name_the_traps() -> None:
    """The instructions exist to prevent specific, known wrong answers."""
    for phrase in [
        "deleted_at IS NULL",
        "list_id IS NOT NULL",
        "message_labels",
        "message_fts MATCH",
        "provider_capabilities",
    ]:
        assert phrase in INSTRUCTIONS


def test_server_registers_a_read_only_profile(corpus: Corpus) -> None:
    """No write tool exists to be called by accident."""
    server = build_server(Config(database=DatabaseConfig(path=corpus.path)))
    import asyncio

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "list_tables",
        "describe_table",
        "execute_query",
        "get_attachment",
    }
    assert not any(
        word in name
        for name in names
        for word in ("send", "reply", "draft", "delete", "archive")
    )


def test_tools_serialize_to_json(corpus: Corpus) -> None:
    """Everything crossing the wire must be JSON-encodable."""
    tools = _tools(corpus)
    for payload in (
        tools.list_tables(),
        tools.describe_table("messages"),
        tools.execute_query("SELECT * FROM messages LIMIT 1"),
    ):
        assert json.loads(json.dumps(payload, default=str))
