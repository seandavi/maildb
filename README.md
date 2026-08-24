# maildb

A local-first, multi-provider email **database** with a safe SQL
interface.

This is deliberately not an "email MCP server." The product is a
normalized SQLite index of your mail that anything can query — agents,
notebooks, n8n, the CLI. MCP is one way to expose it.

> Email accounts are data sources; SQLite is the local indexed
> representation; SQL is the primary read interface.

**Status: M1–M4 built, M3 unverified against a live mailbox.**
249 tests, ruff and mypy-strict clean.

| | Milestone | State |
|---|---|---|
| M1 | Database, FTS, schema metadata | ✅ |
| M2 | SQL safety layer, CLI, client | ✅ |
| M3 | Gmail provider, sync engine | 🟡 code complete, needs a live account |
| M4 | MCP server | ✅ |
| M5 | Apple Mail provider | ⬜ |
| M6 | Writes | ⬜ |
| M7 | Daemon | ⬜ |

- [SPEC.md](SPEC.md) — build specification (v0.2)
- [PLAN.md](PLAN.md) — milestones, exit criteria, open questions
- [decisions/](decisions/) — architecture decision records

```bash
uv sync --all-extras
uv run maildb init
uv run maildb tables
uv run maildb describe messages
uv run maildb query "SELECT subject, sender_email FROM messages LIMIT 5"
uv run maildb search "R01 deadline"
```

## Shape

```text
Gmail API ──┐
            ├──> Sync Engine ──> SQLite + FTS5 ──> SQL safety layer ──┬── MCP
Apple Mail ─┘                                                         ├── REST
(Microsoft 365, via AppleScript)                                      └── CLI
```

Gmail is the reference provider and the schema is built to its fidelity.
The Apple Mail bridge exists for one reason: to reach a Microsoft 365
account without registering a Graph application. It is a stopgap and is
labelled as one ([ADR 0010](decisions/0010-gmail-is-the-reference-provider.md)).

## Design commitments

- **The SQL boundary is enforced by SQLite, not by string matching.**
  Read-only connection, authorizer callback, then a parser for legible
  errors. The security tests must pass with the parser disabled
  ([ADR 0002](decisions/0002-sql-safety-is-authorizer-first.md)).
- **Providers are authoritative for writes.** Never mutate the local
  index and hope the provider catches up.
- **Nothing sends without explicit confirmation.**
- **Loopback is not authentication.** The daemon requires a bearer token
  ([ADR 0007](decisions/0007-loopback-is-not-authentication.md)).
- **A superset schema must not lie.** `provider_capabilities` records
  what each provider can actually do, so an agent gets a stated
  limitation rather than a silently empty result.

## License

MIT
