# 0002 — SQL safety is authorizer-first, parser-second

**Status:** accepted · **Date:** 2026-08-24

## Context

v0.1 section 14 enumerated forbidden keywords (`INSERT`, `DROP`,
`ATTACH`, `PRAGMA`, …). Read literally, that specifies a blocklist.

Blocklists over SQL text lose. They lose to comments (`DR/**/OP`), to
unicode escapes, to case and whitespace variation, to nested CTEs, and
to any construct the author did not think of. Worse, they produce
confidence disproportionate to the protection delivered — which matters
here, because this component runs model-generated SQL against every
message the user has ever received.

## Decision

Three independent layers, ordered by how much they are trusted:

1. **Read-only connection.** `sqlite3.connect("file:…?mode=ro", uri=True)`
   with `enable_load_extension(False)`. Enforced by SQLite beneath any
   SQL the model can emit. No write is reachable even if layers 2 and 3
   fail completely.
2. **Authorizer callback.** Deny every action code except
   `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION`; deny `SQLITE_READ`
   on tables outside the allowlist. This is what actually blocks
   `ATTACH`, `PRAGMA`, and extension loading — by capability, not by
   spelling. `audit_log` is excluded from the read allowlist.
3. **`sqlglot` parse.** Assert one top-level statement, AST root of
   `Select` or `With(Select)`. Purpose is a legible error message.

The security tests in section 25 must pass with layer 3 disabled. A test
that passes only because the parser rejected a string is not testing the
thing that protects the database.

## Consequences

- `sqlglot` becomes a dependency, but not a security-critical one.
- Adding a table to the schema requires adding it to the allowlist,
  which is the correct default (fail closed).
- Query timeouts use `set_progress_handler`, which can interrupt a
  running statement; a wall-clock timer around `execute()` cannot.
