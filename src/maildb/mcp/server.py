"""MCP server — a thin database connector.

The tool surface is deliberately tiny. Three tools that expose SQL beat
forty tools that expose operations: an agent that can write SQL can
answer questions nobody anticipated, and does not need a new tool every
time a question changes shape.

What makes that work is not the tools -- it is the metadata behind
``describe_table``. A model writing SQL against this schema has to know
that ``received_at`` compares correctly as a string, that
``message_labels`` is the authoritative join, and that ``is_important``
is always NULL for one of the two accounts. Those are the difference
between a good answer and a confident wrong one.

Attachment retrieval is the only genuinely non-SQL operation, because
bytes are not rows.

Writes are **not registered** unless explicitly enabled. A read-only
profile is the default, and it is the profile an unattended agent gets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from maildb.config import Config, load_config
from maildb.db.schema import TableInfo, conventions
from maildb.domain.errors import MaildbError
from maildb.service.query import QueryService

log = structlog.get_logger(__name__)

INSTRUCTIONS = """\
A local, multi-provider email database exposed as read-only SQL.

Start with `list_tables`, then `describe_table` on anything you plan to
query -- the column descriptions carry conventions you cannot infer from
types, and the per-provider caveats tell you which columns are NULL for
which account. Guessing the schema wastes a round trip; reading it does not.

Conventions that catch people out:
  * Timestamps are fixed-width ISO-8601 UTC, so `received_at >= '2026-01-01'`
    is correct as written.
  * Filter `deleted_at IS NULL` unless you specifically want deleted mail.
  * Bulk mail is the majority of a real mailbox. `list_id IS NOT NULL` or
    `auto_submitted <> 'no'` identifies it; exclude it unless asked.
  * Join through `message_labels` for mailbox membership, not
    `messages.mailbox_id`.
  * Full-text search: `message_fts MATCH ?` joined on
    `messages.seq = message_fts.rowid`.

Queries are SELECT-only and capped. An empty result may mean the provider
does not support that column rather than that nothing matched -- check
`provider_capabilities` before reporting "none".
"""


def _table_payload(info: TableInfo) -> dict[str, Any]:
    return {
        "name": info.name,
        "description": info.description,
        "columns": [
            {
                "name": column.name,
                "type": column.sql_type,
                "nullable": column.nullable,
                "description": column.description,
                **(
                    {
                        "capability": column.capability,
                        "always_null_for": list(column.null_for),
                    }
                    if column.null_for
                    else {}
                ),
            }
            for column in info.columns
        ],
    }


class MaildbTools:
    """The tool implementations, independent of the MCP transport.

    Kept separate so they can be tested as functions rather than through
    a protocol handshake, and so the REST API can call exactly the same
    code.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.service = QueryService(config.database.path, config.query)

    def list_tables(self) -> dict[str, Any]:
        """Every table available to SQL, with a one-line description."""
        return {
            "tables": [
                {"name": t.name, "description": t.description}
                for t in self.service.list_tables()
            ],
            "conventions": conventions(),
        }

    def describe_table(self, table: str) -> dict[str, Any]:
        """Columns, types, meaning, and per-provider caveats."""
        try:
            info = self.service.describe(table)
        except MaildbError as exc:
            available = ", ".join(t.name for t in self.service.list_tables())
            return {"error": str(exc), "available_tables": available}
        return _table_payload(info)

    def execute_query(self, sql: str) -> dict[str, Any]:
        """Run one read-only SELECT."""
        try:
            result = self.service.execute(sql)
        except MaildbError as exc:
            # Errors go back as data rather than exceptions: a model that
            # can read why it was refused can fix its own query.
            log.info("query.rejected", error=str(exc))
            return {"error": str(exc)}

        payload = result.to_dict()
        if result.truncated:
            payload["note"] = (
                f"Truncated at {self.config.query.max_rows} rows. More rows "
                "matched. Do not present this as a complete result -- narrow "
                "the query or aggregate instead."
            )
        return payload

    def get_attachment(self, message_id: str, filename: str) -> dict[str, Any]:
        """Metadata for one attachment. Does not fetch bytes."""
        rows = self.service.execute(
            "SELECT a.id, a.filename, a.mime_type, a.size_bytes, a.downloaded, "
            "a.local_path FROM attachments a WHERE a.message_id = ? "
            "AND a.filename = ?",
            (message_id, filename),
        )
        if not rows.rows:
            return {"error": f"no attachment {filename!r} on message {message_id}"}
        row = dict(zip(rows.columns, rows.rows[0], strict=True))
        return row


def build_server(
    config: Config | None = None, *, config_path: Path | None = None
) -> Any:
    """Construct the MCP server with the read-only tool profile."""
    from mcp.server import MCPServer

    settings = config or load_config(config_path)
    tools = MaildbTools(settings)

    server = MCPServer(
        name="maildb",
        title="Local email database",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool()
    def list_tables() -> str:
        """List the tables available to SQL, with database-wide conventions.

        Call this first. The conventions it returns explain how timestamps
        compare, how to exclude deleted and bulk mail, and which join is
        authoritative for mailbox membership.
        """
        return json.dumps(tools.list_tables(), indent=2)

    @server.tool()
    def describe_table(table: str) -> str:
        """Describe one table's columns, meanings, and provider caveats.

        Read this before querying a table. `always_null_for` names the
        providers for which a column is never populated -- an empty result
        on such a column means "not supported here", not "none".
        """
        return json.dumps(tools.describe_table(table), indent=2)

    @server.tool()
    def execute_query(sql: str) -> str:
        """Run one read-only SELECT against the mail database.

        SELECT and WITH...SELECT only; results are capped. Prefer explicit
        column lists over SELECT *, and filter `deleted_at IS NULL` unless
        you want deleted mail.
        """
        return json.dumps(tools.execute_query(sql), indent=2, default=str)

    @server.tool()
    def get_attachment(message_id: str, filename: str) -> str:
        """Metadata for one attachment on a message.

        Attachments are indexed as metadata; bytes are fetched only on
        explicit download.
        """
        return json.dumps(tools.get_attachment(message_id, filename), indent=2)

    # Write tools are deliberately absent. Nothing in the read-only
    # profile can change mail, and that is the profile an unattended agent
    # gets (SPEC section 20).
    return server


def main() -> None:  # pragma: no cover - console entry point
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
