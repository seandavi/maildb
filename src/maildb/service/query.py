"""The read interface, and the boundary that makes it safe.

This is the highest-risk component in the system: it executes SQL a
language model wrote, against every message the user has ever received.

It is protected by three layers, and **none of them is a keyword
blocklist**. String matching on ``DROP`` loses to comments, unicode
escapes, and nested CTEs; worse, it produces confidence out of proportion
to the protection delivered.

1. A ``mode=ro`` connection -- enforced by SQLite beneath any SQL.
2. An authorizer denying every capability but allowlisted reads.
3. A ``sqlglot`` parse asserting the statement's shape.

Layer 3 exists to produce a legible error message, not to protect the
database. The security tests run with it disabled, and must still pass
(ADR 0002).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from maildb.config import QueryLimits
from maildb.db import connection
from maildb.db.schema import TableInfo, describe_table, list_tables
from maildb.domain.errors import QueryRejected, ResponseTooLarge


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: float
    #: True when the result hit ``max_rows`` and more rows existed. Callers
    #: must not present a truncated result as a complete one.
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "truncated": self.truncated,
        }


def validate_sql(sql: str) -> str:
    """Assert the statement is a single read, and return it normalized.

    A usability layer. It turns an opaque authorizer denial into a
    sentence explaining what is permitted. It is not what protects the
    database, and nothing here should be relied on for that.
    """
    stripped = sql.strip()
    if not stripped:
        raise QueryRejected("empty query", layer="parser")

    try:
        statements = sqlglot.parse(stripped, read="sqlite")
    except sqlglot.errors.ParseError as exc:
        raise QueryRejected(f"could not parse SQL: {exc}", layer="parser") from exc

    real = [s for s in statements if s is not None]
    if len(real) != 1:
        raise QueryRejected(
            f"expected exactly one statement, got {len(real)}",
            layer="parser",
        )

    statement = real[0]
    if isinstance(statement, exp.Select):
        return stripped
    if isinstance(statement, exp.Union | exp.Except | exp.Intersect):
        return stripped
    if isinstance(statement, exp.Subquery) and isinstance(statement.this, exp.Select):
        return stripped

    kind = type(statement).__name__.upper()
    raise QueryRejected(
        f"only SELECT queries are permitted; got {kind}. "
        "This database is read-only through SQL; use the explicit write "
        "operations for anything that changes state.",
        layer="parser",
    )


class QueryService:
    """Executes read-only SQL under the safety layers described above.

    One instance is the single implementation shared by every transport:
    the CLI, the in-process client, the REST API, and MCP. If those ever
    grow their own query paths, ADR 0009 is void.
    """

    def __init__(self, db_path: Path, limits: QueryLimits | None = None) -> None:
        self.db_path = db_path
        self.limits = limits or QueryLimits()

    def list_tables(self) -> list[TableInfo]:
        return list_tables(self.db_path)

    def describe(self, table: str) -> TableInfo:
        return describe_table(self.db_path, table)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> QueryResult:
        """Run one read-only statement and return capped results."""
        if self.limits.parser_enabled:
            sql = validate_sql(sql)

        conn = connection.read_only(self.db_path)
        started = time.perf_counter()
        try:
            with connection.deadline(conn, self.limits.max_execution_seconds):
                cursor = conn.execute(sql, params)
                # Row cap by fetchmany rather than by rewriting the query:
                # SQLite streams rows, so this stops pulling at the cap
                # without touching SQL we would otherwise have to modify
                # correctly for every query shape.
                fetched = cursor.fetchmany(self.limits.max_rows + 1)
                columns = (
                    [d[0] for d in cursor.description] if cursor.description else []
                )
        except sqlite3.DatabaseError as exc:
            message = str(exc)
            if connection.is_denial(message):
                raise QueryRejected(
                    f"{message}. Readable tables: "
                    f"{', '.join(sorted(t.name for t in self.list_tables()))}.",
                    layer="authorizer",
                ) from exc
            raise QueryRejected(message, layer="sqlite") from exc
        finally:
            conn.close()

        elapsed_ms = (time.perf_counter() - started) * 1000

        truncated = len(fetched) > self.limits.max_rows
        rows = [list(r) for r in fetched[: self.limits.max_rows]]

        result = QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=elapsed_ms,
            truncated=truncated,
        )

        size = len(json.dumps(result.to_dict(), default=str).encode())
        if size > self.limits.max_response_bytes:
            raise ResponseTooLarge(
                f"result is {size} bytes, over the "
                f"{self.limits.max_response_bytes} byte cap. "
                "Select fewer columns, or add a tighter WHERE clause."
            )
        return result
