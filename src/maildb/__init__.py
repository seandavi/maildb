"""maildb — a local-first, multi-provider email database."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"
__all__ = ["Database", "__version__"]

if TYPE_CHECKING:
    from maildb.client import Database


def __getattr__(name: str) -> Any:
    """Import the client lazily.

    Keeps ``import maildb`` cheap, and keeps the package importable while
    optional extras (httpx transports, providers) are absent.
    """
    if name == "Database":
        from maildb.client import Database

        return Database
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
