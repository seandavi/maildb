"""Client transports.

ADR 0009 permits in-process reads *only* while both transports share one
implementation. These tests are what keeps that true.
"""

from __future__ import annotations

from typing import Any

import pytest

from maildb.client import Database
from maildb.config import Config, DatabaseConfig
from maildb.domain.errors import QueryRejected
from maildb.service.query import QueryService
from tests.fixtures.corpus import Corpus


def _local(corpus: Corpus) -> Database:
    return Database(
        local=True,
        config=Config(database=DatabaseConfig(path=corpus.path)),
    )


def test_local_client_reads(corpus: Corpus) -> None:
    db = _local(corpus)
    rows = db.rows("SELECT subject FROM messages WHERE deleted_at IS NULL")
    assert len(rows) == 4
    assert "subject" in rows[0]


def test_local_client_routes_through_the_query_service(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The architecture test behind ADR 0009.

    If a transport ever grows its own execution path, this fails and the
    decision to allow in-process reads is void.
    """
    calls: list[str] = []
    original = QueryService.execute

    def spy(self: QueryService, sql: str, params: tuple[Any, ...] = ()) -> Any:
        calls.append(sql)
        return original(self, sql, params)

    monkeypatch.setattr(QueryService, "execute", spy)
    _local(corpus).query("SELECT 1")
    assert calls == ["SELECT 1"]


def test_local_client_is_not_a_privilege_escalation(corpus: Corpus) -> None:
    """In-process access is exactly as constrained as HTTP access."""
    db = _local(corpus)
    for sql in ("DROP TABLE messages", "SELECT * FROM audit_log"):
        with pytest.raises(QueryRejected):
            db.query(sql)


def test_conventions_are_available_without_a_daemon(corpus: Corpus) -> None:
    assert "timestamps" in _local(corpus).conventions()


def test_http_client_requires_a_token(corpus: Corpus, tmp_path: Any) -> None:
    """Loopback is not authentication (ADR 0007)."""
    config = Config(database=DatabaseConfig(path=corpus.path))
    config.server.token_file = tmp_path / "absent-token"
    db = Database(config=config)
    with pytest.raises(Exception, match="no daemon token"):
        db.query("SELECT 1")
