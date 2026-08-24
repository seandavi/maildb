"""SQLite connection management.

Two factories, deliberately asymmetric:

:func:`writable` is for the sync engine and migrations. Full access,
foreign keys on, WAL.

:func:`read_only` is for anything that executes SQL a model produced. It
opens the file through a ``mode=ro`` URI, disables extension loading, and
installs an authorizer that denies every capability except reading
allowlisted tables. That authorizer -- not any string inspection of the
SQL -- is what actually protects the database (ADR 0002).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Final

from maildb.domain.errors import QueryTimeout

#: Tables an untrusted reader may touch.
#:
#: ``audit_log`` is deliberately absent: a read-only agent should not be
#: able to inspect the record of write operations. yoyo's bookkeeping
#: tables are absent because they are not data.
READABLE_TABLES: Final[frozenset[str]] = frozenset(
    {
        "accounts",
        "provider_capabilities",
        "mailboxes",
        "threads",
        "messages",
        "message_labels",
        "message_headers",
        "attachments",
        "drafts",
        "sync_state",
        "message_fts",
    }
)

#: FTS5 keeps its index in shadow tables. Querying ``message_fts`` reads
#: them, and the authorizer sees those reads, so they must be allowed --
#: without granting anything the reader could not already obtain through
#: the virtual table itself.
_FTS_SHADOW_TABLES: Final[frozenset[str]] = frozenset(
    {
        f"message_fts_{suffix}"
        for suffix in ("data", "idx", "docsize", "config", "content")
    }
)

_ALLOWED_TABLES: Final[frozenset[str]] = READABLE_TABLES | _FTS_SHADOW_TABLES

#: Functions denied outright. ``load_extension`` is already unreachable
#: with extension loading disabled; denying it by name as well costs
#: nothing and documents the intent.
_DENIED_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
    }
)

_ALLOWED_ACTIONS: Final[frozenset[int]] = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)

#: The only pragma an untrusted reader may reach. FTS5 reads
#: ``data_version`` internally on every MATCH, so denying all pragmas
#: outright makes full-text search impossible. It returns an opaque
#: change counter and exposes nothing; reading it is the entire grant,
#: and setting it is still denied.
_ALLOWED_PRAGMAS: Final[frozenset[str]] = frozenset({"data_version"})


def _make_authorizer(real_tables: frozenset[str]) -> Callable[..., int]:
    """Build an authorizer that denies anything but allowlisted reads.

    ``real_tables`` is the set of table names that actually exist on disk.
    It matters because SQLite fires ``SQLITE_READ`` for common table
    expressions and subquery aliases too, using the alias as the table
    name -- so a blanket "deny unless allowlisted" would reject every
    query containing a CTE.

    The distinction that matters is not whether a name is allowlisted but
    whether reading it touches the database: a name that is not a real
    table reads nothing on disk, so it is safe. A CTE that *shadows* a
    real table name is denied anyway, which is a false positive we accept
    in exchange for failing closed.
    """

    def authorize(
        action: int,
        arg1: str | None,
        arg2: str | None,
        db_name: str | None,
        trigger: str | None,
    ) -> int:
        """Fail closed: an action code we have not considered is denied."""
        if action == sqlite3.SQLITE_FUNCTION:
            # For SQLITE_FUNCTION, arg2 carries the function name.
            if arg2 and arg2.lower() in _DENIED_FUNCTIONS:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        if action == sqlite3.SQLITE_READ:
            name = arg1 or ""
            if name.lower().startswith("sqlite_"):
                # Schema tables. Structure is served by describe_table,
                # which does not run model-supplied SQL.
                return sqlite3.SQLITE_DENY
            if name in _ALLOWED_TABLES:
                return sqlite3.SQLITE_OK
            if name in real_tables:
                return sqlite3.SQLITE_DENY
            # Not a table on disk: a CTE or subquery alias.
            return sqlite3.SQLITE_OK

        if action == sqlite3.SQLITE_PRAGMA:
            # arg1 is the pragma name, arg2 the value being set. A pragma
            # carrying a value is a write, whatever it is called.
            if arg1 and arg1.lower() in _ALLOWED_PRAGMAS and arg2 is None:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        if action in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_OK

        return sqlite3.SQLITE_DENY

    return authorize


def _real_tables(conn: sqlite3.Connection) -> frozenset[str]:
    """Names of tables and views that actually exist."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return frozenset(row[0] for row in rows)


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")


def writable(path: Path) -> sqlite3.Connection:
    """Open a read-write connection. Never used for model-supplied SQL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _configure(conn)
    return conn


def introspection(path: Path) -> sqlite3.Connection:
    """Open a read-only connection *without* the authorizer.

    Used for ``PRAGMA table_info`` and similar schema reads that back
    ``describe_table``. This connection never executes model-supplied
    SQL; keeping it separate is what lets the query connection deny
    PRAGMA outright.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def read_only(path: Path) -> sqlite3.Connection:
    """Open a connection safe to execute model-supplied SQL against.

    Three properties, in descending order of how much they are trusted:

    1. ``mode=ro`` -- enforced by SQLite beneath any SQL. No write is
       reachable even if everything above fails.
    2. Extension loading disabled.
    3. An authorizer denying every capability but allowlisted reads.
    """
    if not path.exists():
        raise FileNotFoundError(f"no database at {path}")

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    with suppress(AttributeError):  # builds without extension support
        conn.enable_load_extension(False)
    # Read the schema *before* installing the authorizer -- afterwards
    # sqlite_master is denied, which is the point.
    conn.set_authorizer(_make_authorizer(_real_tables(conn)))
    return conn


def is_denial(message: str) -> bool:
    """Whether a SQLite error is an authorizer denial.

    SQLite phrases these two ways -- "not authorized" for statement-level
    denials, "access to X.Y is prohibited" for column reads -- and both
    must map to the same rejection.
    """
    lowered = message.lower()
    return "not authorized" in lowered or "is prohibited" in lowered


@contextmanager
def deadline(conn: sqlite3.Connection, seconds: float) -> Iterator[None]:
    """Interrupt a query that outruns its budget.

    Uses ``set_progress_handler`` rather than a wall-clock timer around
    ``execute()``: a timer can only observe that a query is slow, while
    the progress handler can actually stop it mid-execution and stop it
    burning CPU.
    """
    expiry = time.monotonic() + seconds
    tripped = False

    def handler() -> int:
        nonlocal tripped
        if time.monotonic() > expiry:
            tripped = True
            return 1  # non-zero aborts the running statement
        return 0

    # Checked every N virtual-machine instructions. Small enough to be
    # responsive, large enough not to dominate the query cost.
    conn.set_progress_handler(handler, 1000)
    try:
        yield
    except sqlite3.OperationalError as exc:
        if tripped or "interrupted" in str(exc).lower():
            raise QueryTimeout(
                f"query exceeded its {seconds:g}s execution budget"
            ) from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
