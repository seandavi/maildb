"""Command-line interface.

The CLI is a transport over the same application service as MCP and the
REST API, not a parallel implementation. Every command here is something
an agent can do too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from maildb.config import Config, load_config
from maildb.db.migrations import applied_migrations, apply_migrations
from maildb.db.schema import conventions
from maildb.db.search import SearchFilters, build_search
from maildb.domain.errors import MaildbError
from maildb.service.query import QueryService

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local-first multi-provider email database.",
)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to config.toml."),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON.")]

AccountOption = Annotated[str | None, typer.Option(help="Restrict to one account.")]
SenderOption = Annotated[
    str | None, typer.Option(help="Substring of the sender address.")
]
SinceOption = Annotated[str | None, typer.Option(help="ISO date lower bound.")]
UnreadOption = Annotated[bool, typer.Option("--unread", help="Unread only.")]
BulkOption = Annotated[
    bool,
    typer.Option("--bulk", help="Include newsletters and automated mail."),
]


def _config(path: Path | None) -> Config:
    return load_config(path)


def _service(path: Path | None) -> QueryService:
    config = _config(path)
    if not config.database.path.exists():
        raise typer.BadParameter(
            f"no database at {config.database.path}. Run 'maildb init' first."
        )
    return QueryService(config.database.path, config.query)


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _print_table(columns: list[str], rows: list[list[object]]) -> None:
    """Render results as aligned columns, truncating wide cells."""
    if not columns:
        return
    cells = [[_cell(v) for v in row] for row in rows]
    widths = [
        min(max([len(c)] + [len(r[i]) for r in cells]), 60)
        for i, c in enumerate(columns)
    ]
    header = "  ".join(c[:w].ljust(w) for c, w in zip(columns, widths, strict=True))
    typer.echo(header)
    typer.echo("  ".join("-" * w for w in widths))
    for row in cells:
        typer.echo("  ".join(v[:w].ljust(w) for v, w in zip(row, widths, strict=True)))


def _cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ")


@app.command()
def init(config: ConfigOption = None) -> None:
    """Create the database and bring it to the latest schema."""
    settings = _config(config)
    applied = apply_migrations(settings.database.path)
    if applied:
        typer.echo(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        typer.echo("schema already up to date")
    typer.echo(f"database: {settings.database.path}")


@app.command()
def accounts(config: ConfigOption = None, as_json: JsonOption = False) -> None:
    """List configured accounts and their sync state."""
    service = _service(config)
    result = service.execute(
        "SELECT a.id, a.provider, a.email, a.enabled, s.status, "
        "s.last_sync_completed FROM accounts a "
        "LEFT JOIN sync_state s ON s.account_id = a.id ORDER BY a.id"
    )
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        _print_table(result.columns, result.rows)


@app.command()
def tables(config: ConfigOption = None, as_json: JsonOption = False) -> None:
    """List the tables available to SQL."""
    service = _service(config)
    infos = service.list_tables()
    if as_json:
        payload = [{"name": t.name, "description": t.description} for t in infos]
        typer.echo(json.dumps({"tables": payload}, indent=2))
        return
    _print_table(
        ["table", "description"],
        [[t.name, t.description] for t in infos],
    )


@app.command()
def describe(
    table: str, config: ConfigOption = None, as_json: JsonOption = False
) -> None:
    """Describe a table's columns, including provider caveats."""
    service = _service(config)
    try:
        info = service.describe(table)
    except MaildbError as exc:
        _fail(str(exc))
        return

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "name": info.name,
                    "description": info.description,
                    "columns": [
                        {
                            "name": c.name,
                            "type": c.sql_type,
                            "nullable": c.nullable,
                            "description": c.description,
                            "capability": c.capability,
                            "null_for": list(c.null_for),
                        }
                        for c in info.columns
                    ],
                },
                indent=2,
            )
        )
        return

    typer.secho(info.name, bold=True)
    typer.echo(info.description)
    typer.echo()
    # The capability caveat gets its own column rather than a suffix on
    # the description: it is the single most consequential thing on the
    # row, and appending it means the cell truncation eats it.
    rows: list[list[object]] = [
        [
            column.name,
            column.sql_type,
            ", ".join(column.null_for),
            column.description,
        ]
        for column in info.columns
    ]
    _print_table(["column", "type", "always NULL for", "description"], rows)


@app.command(name="conventions")
def conventions_(
    config: ConfigOption = None,
) -> None:
    """Print database-wide conventions worth knowing before writing SQL."""
    for name, text in conventions().items():
        typer.secho(name, bold=True)
        typer.echo(f"  {text}")
        typer.echo()


@app.command()
def query(sql: str, config: ConfigOption = None, as_json: JsonOption = False) -> None:
    """Run a read-only SQL query."""
    service = _service(config)
    try:
        result = service.execute(sql)
    except MaildbError as exc:
        _fail(str(exc))
        return

    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2, default=str))
        return

    _print_table(result.columns, result.rows)
    suffix = " (truncated)" if result.truncated else ""
    typer.secho(
        f"\n{result.row_count} row(s) in {result.elapsed_ms:.1f}ms{suffix}",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def search(
    terms: str,
    account: AccountOption = None,
    sender: SenderOption = None,
    since: SinceOption = None,
    unread: UnreadOption = False,
    bulk: BulkOption = False,
    limit: Annotated[int, typer.Option(help="Maximum results.")] = 25,
    config: ConfigOption = None,
    as_json: JsonOption = False,
) -> None:
    """Full-text search across all accounts.

    Bulk mail is excluded unless --bulk: it is the majority of any real
    mailbox and rarely what anyone is looking for.
    """
    service = _service(config)
    filters = SearchFilters(
        account_id=account,
        sender=sender,
        since=f"{since}T00:00:00Z" if since and len(since) == 10 else since,
        is_read=False if unread else None,
        include_bulk=bulk,
        limit=limit,
    )
    try:
        sql, params = build_search(terms, filters)
        result = service.execute(sql, tuple(params))
    except MaildbError as exc:
        _fail(str(exc))
        return

    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2, default=str))
        return

    _print_table(
        ["received", "from", "subject"],
        [[r[5], r[4], r[2]] for r in result.rows],
    )
    typer.secho(f"\n{result.row_count} result(s)", fg=typer.colors.BRIGHT_BLACK)


@app.command()
def auth(
    account: str,
    config: ConfigOption = None,
    scopes: Annotated[
        str | None,
        typer.Option(help="Comma-separated scopes. Defaults to read-only."),
    ] = None,
) -> None:
    """Authorize an account, opening a browser for consent.

    Run once per account per machine. The refresh token is written to the
    Keychain; the OAuth client itself comes from Secret Manager.
    """
    from maildb.accounts import authorize

    settings = _config(config)
    try:
        account_config = settings.account(account)
    except MaildbError as exc:
        _fail(str(exc))
        return

    scope_list = [s.strip() for s in scopes.split(",")] if scopes else None
    typer.echo(f"authorizing {account_config.email} ({account_config.id})...")
    typer.echo("a browser window will open for consent")
    try:
        authorize(settings, account_config, scopes=scope_list)
    except Exception as exc:  # surfaced verbatim: the causes are all external
        _fail(f"authorization failed: {exc}")
        return
    typer.secho(
        "authorized; refresh token stored in the Keychain", fg=typer.colors.GREEN
    )


@app.command()
def sync(
    account: Annotated[
        str | None, typer.Argument(help="Account id; omit for all.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Synchronize one account, or every enabled account."""
    from maildb.accounts import build_provider
    from maildb.sync.engine import SyncEngine

    settings = _config(config)
    if not settings.database.path.exists():
        _fail(f"no database at {settings.database.path}. Run 'maildb init' first.")
        return

    targets = (
        [settings.account(account)]
        if account
        else [a for a in settings.accounts if a.enabled and a.sync_enabled]
    )
    if not targets:
        _fail("no enabled accounts configured")
        return

    engine = SyncEngine(settings.database.path)
    failures = 0
    for target in targets:
        typer.echo(f"syncing {target.id} ({target.email})...")
        engine.ensure_account(
            target.id,
            target.provider.value,
            target.email,
            auth_method=target.auth_method.value,
            credential_ref=target.credential_ref,
        )
        try:
            provider = build_provider(settings, target)
            stats = engine.sync_account(provider, target.id)
        except Exception as exc:
            failures += 1
            typer.secho(f"  failed: {exc}", fg=typer.colors.RED, err=True)
            continue
        typer.secho(
            f"  {stats.added} added, {stats.updated} updated, "
            f"{stats.deleted} deleted, {stats.mailboxes} mailboxes",
            fg=typer.colors.GREEN,
        )
    if failures:
        raise typer.Exit(1)


@app.command()
def status(config: ConfigOption = None) -> None:
    """Show database and sync status."""
    settings = _config(config)
    if not settings.database.path.exists():
        typer.echo(f"no database at {settings.database.path}")
        raise typer.Exit(1)

    service = QueryService(settings.database.path, settings.query)
    counts = service.execute(
        "SELECT (SELECT count(*) FROM messages WHERE deleted_at IS NULL), "
        "(SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL), "
        "(SELECT count(*) FROM threads), (SELECT count(*) FROM accounts)"
    ).rows[0]
    size_mb = settings.database.path.stat().st_size / 1_048_576

    typer.echo(f"database   {settings.database.path} ({size_mb:.1f} MB)")
    migrations = applied_migrations(settings.database.path)
    typer.echo(f"schema     {len(migrations)} migration(s)")
    typer.echo(f"accounts   {counts[3]}")
    typer.echo(f"messages   {counts[0]} live, {counts[1]} deleted")
    typer.echo(f"threads    {counts[2]}")
    typer.echo()

    sync = service.execute(
        "SELECT account_id, status, last_sync_completed, last_reconciled_at "
        "FROM sync_state ORDER BY account_id"
    )
    if sync.rows:
        _print_table(sync.columns, sync.rows)


def main() -> None:  # pragma: no cover - console entry point
    try:
        app()
    except MaildbError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
