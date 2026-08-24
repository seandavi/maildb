# 0006 — All timestamps are fixed-width ISO-8601 UTC

**Status:** accepted · **Date:** 2026-08-24

## Context

v0.1 typed every timestamp as `TEXT` without specifying a format. SQLite
compares TEXT lexicographically. An agent will write
`WHERE received_at >= '2026-01-01'` on its first attempt — that is
correct only if every stored value is fixed-width, UTC, and
zero-padded. Mixed offsets or variable precision produce silently wrong
date filters.

## Decision

Every timestamp column stores `YYYY-MM-DDTHH:MM:SSZ`. UTC always.
Normalization happens on write, in the provider adapter, never on read.
A `CHECK` constraint enforces length and the trailing `Z`.

For Gmail, `internal_date` (from `internalDate`) is the authoritative
ordering key. The `Date:` header is client-supplied and routinely wrong
or forged; it is retained in `message_headers` but not trusted.

## Consequences

- Lexicographic ordering equals chronological ordering, so plain
  B-tree indexes serve range queries.
- Local-time questions ("what arrived Tuesday morning?") require
  conversion at query time. Documented in schema metadata.
