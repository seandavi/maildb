"""CLI behaviour. The CLI is a transport, not a second implementation."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from maildb.cli import app
from tests.fixtures.corpus import Corpus

runner = CliRunner()


def _config_file(tmp_path: Path, db_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[database]\npath = "{db_path}"\n')
    return path


def test_init_creates_and_is_idempotent(tmp_path: Path) -> None:
    config = _config_file(tmp_path, tmp_path / "mail.db")
    first = runner.invoke(app, ["init", "-c", str(config)])
    assert first.exit_code == 0
    assert "applied" in first.stdout

    second = runner.invoke(app, ["init", "-c", str(config)])
    assert second.exit_code == 0
    assert "already up to date" in second.stdout


def test_tables_lists_readable_tables(tmp_path: Path, corpus: Corpus) -> None:
    config = _config_file(tmp_path, corpus.path)
    result = runner.invoke(app, ["tables", "-c", str(config), "--json"])
    assert result.exit_code == 0
    names = {t["name"] for t in json.loads(result.stdout)["tables"]}
    assert "messages" in names
    assert "audit_log" not in names


def test_describe_surfaces_provider_caveats(tmp_path: Path, corpus: Corpus) -> None:
    config = _config_file(tmp_path, corpus.path)
    result = runner.invoke(app, ["describe", "messages", "-c", str(config)])
    assert result.exit_code == 0
    # The caveat must survive column truncation -- it is the most
    # consequential thing on the row.
    assert "always NULL for" in result.stdout
    assert "apple_mail" in result.stdout


def test_query_runs_and_reports_timing(tmp_path: Path, corpus: Corpus) -> None:
    config = _config_file(tmp_path, corpus.path)
    result = runner.invoke(
        app, ["query", "SELECT subject FROM messages", "-c", str(config)]
    )
    assert result.exit_code == 0
    assert "row(s) in" in result.stdout


def test_query_refuses_a_write(tmp_path: Path, corpus: Corpus) -> None:
    config = _config_file(tmp_path, corpus.path)
    result = runner.invoke(app, ["query", "DROP TABLE messages", "-c", str(config)])
    assert result.exit_code == 1


def test_search_excludes_bulk_by_default(tmp_path: Path, corpus: Corpus) -> None:
    config = _config_file(tmp_path, corpus.path)
    plain = runner.invoke(app, ["search", "preprints", "-c", str(config)])
    assert "0 result(s)" in plain.stdout

    with_bulk = runner.invoke(app, ["search", "preprints", "--bulk", "-c", str(config)])
    assert "1 result(s)" in with_bulk.stdout


def test_status_reports_counts(tmp_path: Path, corpus: Corpus) -> None:
    config = _config_file(tmp_path, corpus.path)
    result = runner.invoke(app, ["status", "-c", str(config)])
    assert result.exit_code == 0
    assert "4 live, 1 deleted" in result.stdout


def test_commands_fail_clearly_without_a_database(tmp_path: Path) -> None:
    config = _config_file(tmp_path, tmp_path / "absent.db")
    result = runner.invoke(app, ["query", "SELECT 1", "-c", str(config)])
    assert result.exit_code != 0
    assert "maildb init" in result.output
