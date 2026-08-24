from __future__ import annotations

from pathlib import Path

import pytest

from maildb.config import QueryLimits
from maildb.service.query import QueryService
from tests.fixtures import corpus as corpus_module


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "mail.db"


@pytest.fixture
def corpus(db_path: Path) -> corpus_module.Corpus:
    return corpus_module.build(db_path)


@pytest.fixture
def service(corpus: corpus_module.Corpus) -> QueryService:
    return QueryService(corpus.path)


@pytest.fixture
def unparsed_service(corpus: corpus_module.Corpus) -> QueryService:
    """A service with the parser disabled.

    The security suite runs against this so that every rejection is
    demonstrably the authorizer's doing (ADR 0002). If a test passes only
    because sqlglot refused the string, it is not testing what actually
    protects the database.
    """
    return QueryService(corpus.path, QueryLimits(parser_enabled=False))
