"""Schema migration, wrapping yoyo.

yoyo owns versioning, ordering, and its own bookkeeping tables. We own
only the question of where the migration directory is.
"""

from __future__ import annotations

from pathlib import Path

from yoyo import get_backend, read_migrations

from maildb.domain.errors import MigrationError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def _backend_uri(path: Path) -> str:
    return f"sqlite:///{path}"


def apply_migrations(path: Path, *, migrations_dir: Path | None = None) -> list[str]:
    """Bring the database at ``path`` up to the latest schema.

    Returns the ids of migrations that were applied, so callers can log
    or assert on a no-op.
    """
    directory = migrations_dir or MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(f"no migrations directory at {directory}")

    path.parent.mkdir(parents=True, exist_ok=True)
    backend = get_backend(_backend_uri(path))
    migrations = read_migrations(str(directory))

    with backend.lock():
        pending = backend.to_apply(migrations)
        applied = [m.id for m in pending]
        backend.apply_migrations(pending)
    return applied


def rollback_migrations(path: Path, *, migrations_dir: Path | None = None) -> list[str]:
    """Roll every applied migration back. Used by tests, not in anger."""
    directory = migrations_dir or MIGRATIONS_DIR
    backend = get_backend(_backend_uri(path))
    migrations = read_migrations(str(directory))

    with backend.lock():
        pending = backend.to_rollback(migrations)
        rolled = [m.id for m in pending]
        backend.rollback_migrations(pending)
    return rolled


def applied_migrations(path: Path, *, migrations_dir: Path | None = None) -> list[str]:
    directory = migrations_dir or MIGRATIONS_DIR
    backend = get_backend(_backend_uri(path))
    migrations = read_migrations(str(directory))
    return [m.id for m in migrations if backend.is_applied(m)]
