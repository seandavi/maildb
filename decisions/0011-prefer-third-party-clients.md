# 0011 — Prefer maintained libraries over hand-rolled clients

**Status:** accepted · **Date:** 2026-08-24

## Context

A project of this shape accretes bespoke clients quickly: an OAuth
refresh loop, a MIME walker, a header decoder, a migration runner, an
MCP protocol handler. Each is straightforward to write and permanent to
maintain, and none of them is where this project's value lives.

The differentiated work is the canonical schema, the SQL safety layer,
and provider normalization. Everything else is plumbing that someone
else already maintains better.

## Decision

Standing rule: prefer a maintained third-party library. The dependency
table in SPEC.md section 4.1 is normative — adding a hand-rolled
equivalent of anything listed there requires a new ADR superseding this
one.

Notably: `google-api-python-client` for Gmail (token refresh, DWD
impersonation, batching, retry), the official `mcp` SDK for the agent
protocol, `yoyo-migrations` for schema versioning, `sqlglot` for SQL
parsing, `keyring` for Keychain access, and stdlib `email` for
everything MIME.

Three exceptions, recorded in section 4.1: `osascript` via `subprocess`
rather than `appscript` (which breaks across macOS releases); in-repo
thread reconstruction (`jwzthreading` is unmaintained, and the logic is
core domain, ~50 lines); and synchronous provider adapters wrapped in
`asyncio.to_thread`.

## Consequences

- Larger dependency tree, pinned via `uv.lock`. Acceptable — this is a
  local single-user service, not a distributed library.
- The async house style is honored at the service seam rather than all
  the way down. Documented so it does not read as an oversight.
- Dependency review becomes a real maintenance task: `uv lock --upgrade`
  on a schedule, not never.
