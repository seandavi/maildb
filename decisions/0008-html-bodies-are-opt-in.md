# 0008 — `body_html` retention is off by default

**Status:** accepted · **Date:** 2026-08-24

## Context

HTML bodies dominate message size — typically 5–20× the plain-text part
once tracking markup and inlined styling are counted. At roughly 50k
messages, retaining HTML is on the order of a gigabyte. None of it is
full-text indexed; `message_fts` covers `body_text` only.

## Decision

`sync.retain_html = false` by default. `body_html` stays NULL unless
explicitly enabled. `body_text` is always populated — synthesized from
HTML when a message has no plain-text part.

## Consequences

- Rendering an original message requires re-fetching from the provider
  unless retention is on. Acceptable: this is a query database, not a
  mail client.
- Flipping the flag on affects newly synced messages only; backfilling
  historical HTML requires an explicit re-sync.
