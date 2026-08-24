"""Search composition: FTS syntax for terms, SQL for filters."""

from __future__ import annotations

from contextlib import suppress

import pytest

from maildb.db.search import SearchFilters, build_search
from maildb.domain.errors import QueryRejected
from maildb.service.query import QueryService


def _subjects(service: QueryService, query: str, **kwargs: object) -> list[str]:
    sql, params = build_search(query, SearchFilters(**kwargs))  # type: ignore[arg-type]
    return [row[2] for row in service.execute(sql, tuple(params)).rows]


def test_keyword_search(service: QueryService) -> None:
    assert _subjects(service, "resubmission") == [
        "R01 resubmission timeline",
        "Re: R01 resubmission timeline",
    ]


def test_phrase_search(service: QueryService) -> None:
    assert _subjects(service, '"specific aims"') == ["R01 resubmission timeline"]


def test_boolean_operators(service: QueryService) -> None:
    assert len(_subjects(service, "agenda OR resubmission")) == 3
    assert _subjects(service, "meeting NOT cancelled") == [
        "DBMI faculty meeting agenda"
    ]


def test_prefix_search(service: QueryService) -> None:
    assert _subjects(service, "resubmis*") == [
        "R01 resubmission timeline",
        "Re: R01 resubmission timeline",
    ]


def test_bulk_mail_is_excluded_by_default(service: QueryService) -> None:
    """The default that makes search useful on a real mailbox."""
    assert _subjects(service, "preprints") == []
    assert _subjects(service, "preprints", include_bulk=True) == [
        "This week in computational biology"
    ]


def test_deleted_mail_is_excluded_by_default(service: QueryService) -> None:
    assert _subjects(service, "cancelled") == []
    assert _subjects(service, "cancelled", include_deleted=True) == [
        "Cancelled: retreat planning"
    ]


def test_account_filter(service: QueryService) -> None:
    assert _subjects(service, "agenda", account_id="gmail-personal") == []
    assert _subjects(service, "agenda", account_id="cu") == [
        "DBMI faculty meeting agenda"
    ]


def test_sender_filter(service: QueryService) -> None:
    assert _subjects(service, "resubmission", sender="casey") == [
        "R01 resubmission timeline"
    ]


def test_date_filter(service: QueryService) -> None:
    """Plain string comparison, which the timestamp convention makes correct."""
    assert _subjects(service, "resubmission", since="2026-08-21T00:00:00Z") == [
        "Re: R01 resubmission timeline"
    ]


def test_unread_filter(service: QueryService) -> None:
    assert _subjects(service, "resubmission", is_read=False) == [
        "R01 resubmission timeline"
    ]


def test_mailbox_filter_goes_through_the_junction_table(
    service: QueryService,
) -> None:
    """message_labels is authoritative for both provider shapes (ADR 0003)."""
    assert _subjects(service, "resubmission", mailbox="IMPORTANT") == [
        "R01 resubmission timeline"
    ]


def test_search_terms_cannot_escape_into_sql(service: QueryService) -> None:
    """Terms go into FTS5's query language, never into SQL syntax."""
    sql, params = build_search("'; DROP TABLE messages; --", SearchFilters())
    # FTS5 may reject this as malformed MATCH syntax; what must not happen
    # is that it executes as SQL.
    with suppress(QueryRejected):
        service.execute(sql, tuple(params))
    assert service.execute("SELECT count(*) FROM messages").rows[0][0] == 5


@pytest.mark.parametrize("query", ["", "   ", "AND", "OR"])
def test_degenerate_queries_are_refused(query: str) -> None:
    with pytest.raises(QueryRejected):
        build_search(query, SearchFilters())
