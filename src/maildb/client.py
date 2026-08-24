"""The Python client.

Two transports, one implementation:

``Database()``
    talks to the local daemon over HTTP.

``Database(local=True)``
    opens the database file directly.

``local=True`` does **not** bypass the safety layer -- it constructs the
same :class:`~maildb.service.query.QueryService`, with the same read-only
connection and the same authorizer. The security boundary is the
authorizer, not the process boundary, so an in-process reader is exactly
as constrained as an HTTP one while notebooks keep working when no daemon
is running.

That equivalence is the entire justification (ADR 0009). If the two
transports ever grow separate query paths, the decision is void.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maildb.config import Config, load_config
from maildb.db.schema import TableInfo, conventions
from maildb.domain.errors import MaildbError
from maildb.service.query import QueryResult, QueryService


class Database:
    """Read access to the mail database."""

    def __init__(
        self,
        *,
        local: bool = False,
        config: Config | None = None,
        config_path: Path | None = None,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.config = config or load_config(config_path)
        self.local = local
        self._service: QueryService | None = None
        self._base_url = base_url or (
            f"http://{self.config.server.bind}:{self.config.server.port}"
        )
        self._token = token

    # -- shared implementation -------------------------------------------

    @property
    def service(self) -> QueryService:
        """The single query implementation, shared by both transports."""
        if self._service is None:
            self._service = QueryService(self.config.database.path, self.config.query)
        return self._service

    def _auth_headers(self) -> dict[str, str]:
        token = self._token
        if token is None:
            token_file = self.config.server.token_file
            if not token_file.exists():
                raise MaildbError(
                    f"no daemon token at {token_file}. Start the daemon, or "
                    "use Database(local=True) for direct access."
                )
            token = token_file.read_text().strip()
        return {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        response = httpx.post(
            f"{self._base_url}{path}",
            json=payload,
            headers=self._auth_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    def _get(self, path: str) -> dict[str, Any]:
        import httpx

        response = httpx.get(
            f"{self._base_url}{path}", headers=self._auth_headers(), timeout=30.0
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    # -- public API ------------------------------------------------------

    def conventions(self) -> dict[str, str]:
        """Database-wide conventions worth knowing before writing SQL."""
        return conventions()

    def list_tables(self) -> list[TableInfo] | list[dict[str, Any]]:
        if self.local:
            return self.service.list_tables()
        return list(self._get("/v1/tables")["tables"])

    def describe(self, table: str) -> TableInfo | dict[str, Any]:
        if self.local:
            return self.service.describe(table)
        return self._get(f"/v1/tables/{table}")

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> QueryResult:
        """Execute one read-only statement."""
        if self.local:
            return self.service.execute(sql, params)
        payload = self._post("/v1/query", {"sql": sql, "params": list(params)})
        return QueryResult(
            columns=payload["columns"],
            rows=payload["rows"],
            row_count=payload["row_count"],
            elapsed_ms=payload["elapsed_ms"],
            truncated=payload.get("truncated", False),
        )

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Like :meth:`query`, but as a list of dicts."""
        result = self.query(sql, params)
        return [dict(zip(result.columns, row, strict=True)) for row in result.rows]
