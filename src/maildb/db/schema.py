"""Semantic schema metadata.

SQLite knows a column is TEXT. It does not know that ``received_at`` sorts
chronologically as a string, or that ``is_important`` is always NULL for
one of the two providers. That gap is most of what separates good
agent-written SQL from plausible-looking wrong answers, so it is
maintained here rather than inferred.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from maildb.db.connection import READABLE_TABLES
from maildb.domain.errors import MaildbError

METADATA_PATH = Path(__file__).with_name("metadata.yaml")


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    sql_type: str
    nullable: bool
    description: str
    #: Provider capability governing this column, if it is not universal.
    capability: str | None = None
    #: Providers for which this column is never populated.
    null_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableInfo:
    name: str
    description: str
    columns: list[ColumnInfo] = field(default_factory=list)


@lru_cache(maxsize=1)
def _raw_metadata() -> dict[str, Any]:
    with METADATA_PATH.open("rb") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise MaildbError(f"malformed schema metadata at {METADATA_PATH}")
    return data


def conventions() -> dict[str, str]:
    """Database-wide conventions an agent needs before writing any SQL."""
    return dict(_raw_metadata().get("conventions", {}))


def _column_entry(value: Any) -> tuple[str, str | None]:
    """Normalize a metadata entry to ``(description, capability)``.

    Entries are either a bare string or a mapping with an explicit
    capability, so the common case stays terse.
    """
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        return str(value.get("description", "")), value.get("capability")
    return str(value), None


@lru_cache(maxsize=32)
def _capability_gaps(db_path: Path) -> dict[str, tuple[str, ...]]:
    """Map each capability to the providers that lack it."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT capability, provider FROM provider_capabilities WHERE supported = 0"
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    finally:
        conn.close()

    gaps: dict[str, list[str]] = {}
    for capability, provider in rows:
        gaps.setdefault(capability, []).append(provider)
    return {k: tuple(sorted(v)) for k, v in gaps.items()}


def list_tables(db_path: Path) -> list[TableInfo]:
    """Every table an untrusted reader may query, with its description."""
    meta = _raw_metadata()["tables"]
    return [
        TableInfo(name=name, description=str(meta[name]["description"]).strip())
        for name in sorted(READABLE_TABLES)
        if name in meta
    ]


def describe_table(db_path: Path, table: str) -> TableInfo:
    """Column-level description, including provider capability caveats.

    Types and nullability come from SQLite; meaning comes from the
    metadata file; the ``null_for`` annotation is derived by joining the
    two against ``provider_capabilities``. That last part is what stops
    the superset schema from silently misleading a caller.
    """
    if table not in READABLE_TABLES:
        raise MaildbError(f"unknown or non-readable table: {table!r}")

    meta = _raw_metadata()["tables"].get(table)
    if meta is None:
        raise MaildbError(f"no schema metadata for table {table!r}")

    gaps = _capability_gaps(db_path)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()

    # Virtual tables report no columns through table_info in some builds;
    # fall back to the metadata file, which lists them explicitly.
    if not rows:
        rows = [(i, name, "TEXT", 0, None, 0) for i, name in enumerate(meta["columns"])]

    columns = []
    for _cid, name, sql_type, notnull, _default, _pk in rows:
        entry = meta["columns"].get(name)
        if entry is None:
            continue
        description, capability = _column_entry(entry)
        columns.append(
            ColumnInfo(
                name=name,
                sql_type=sql_type or "TEXT",
                nullable=not notnull,
                description=description.strip(),
                capability=capability,
                null_for=gaps.get(capability, ()) if capability else (),
            )
        )

    return TableInfo(
        name=table,
        description=str(meta["description"]).strip(),
        columns=columns,
    )


def documented_columns() -> dict[str, set[str]]:
    """Tables to documented column names, for the coverage test."""
    return {
        table: set(spec["columns"]) for table, spec in _raw_metadata()["tables"].items()
    }
