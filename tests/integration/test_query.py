"""Query execution: limits, truncation, and shape."""

from __future__ import annotations

import pytest

from maildb.config import QueryLimits
from maildb.domain.errors import QueryRejected, QueryTimeout, ResponseTooLarge
from maildb.service.query import QueryService, validate_sql


def test_select_returns_columns_and_rows(service: QueryService) -> None:
    result = service.execute("SELECT id, subject FROM messages ORDER BY received_at")
    assert result.columns == ["id", "subject"]
    assert result.row_count == 5
    assert result.elapsed_ms >= 0


def test_cte_is_permitted(service: QueryService) -> None:
    result = service.execute(
        "WITH recent AS (SELECT * FROM messages WHERE received_at >= '2026-08-21') "
        "SELECT count(*) FROM recent"
    )
    assert result.rows[0][0] == 3


def test_row_cap_truncates_and_says_so(corpus: object, service: QueryService) -> None:
    capped = QueryService(service.db_path, QueryLimits(max_rows=2))
    result = capped.execute("SELECT id FROM messages")
    assert result.row_count == 2
    assert result.truncated is True


def test_row_cap_ignores_a_larger_limit_in_the_query(service: QueryService) -> None:
    """The cap is ours, not the query's."""
    capped = QueryService(service.db_path, QueryLimits(max_rows=1))
    result = capped.execute("SELECT id FROM messages LIMIT 100")
    assert result.row_count == 1
    assert result.truncated is True


def test_untruncated_result_is_not_flagged(service: QueryService) -> None:
    result = service.execute("SELECT id FROM messages")
    assert result.truncated is False


def test_runaway_query_is_interrupted(service: QueryService) -> None:
    """A wall-clock timer could only notice; the progress handler stops it."""
    impatient = QueryService(service.db_path, QueryLimits(max_execution_seconds=0.2))
    with pytest.raises(QueryTimeout):
        impatient.execute(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c"
        )


def test_oversized_response_is_refused(service: QueryService) -> None:
    tiny = QueryService(service.db_path, QueryLimits(max_response_bytes=64))
    with pytest.raises(ResponseTooLarge):
        tiny.execute("SELECT * FROM messages")


@pytest.mark.parametrize(
    "sql",
    ["", "   ", "SELECT * FROM", "this is not sql at all ((("],
)
def test_malformed_sql_is_rejected_by_the_parser(
    service: QueryService, sql: str
) -> None:
    with pytest.raises(QueryRejected) as exc:
        service.execute(sql)
    assert exc.value.layer == "parser"


def test_parser_error_explains_what_is_allowed() -> None:
    with pytest.raises(QueryRejected) as exc:
        validate_sql("DELETE FROM messages")
    assert "only SELECT" in str(exc.value)


def test_union_is_allowed() -> None:
    assert validate_sql("SELECT 1 UNION SELECT 2")
