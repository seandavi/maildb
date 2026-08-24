# 0001 — `messages` gets an integer surrogate key

**Status:** accepted · **Date:** 2026-08-24

## Context

The v0.1 schema declared `messages.id TEXT PRIMARY KEY` and an FTS5
external-content table with `content_rowid='rowid'`.

A SQLite table whose primary key is `TEXT` still has an implicit rowid,
but that rowid is not stable: `VACUUM` may renumber the rowids of any
table lacking an `INTEGER PRIMARY KEY`. The FTS index stores those
rowids. A single `VACUUM` therefore silently decouples every search
result from its row — no error, no corruption flag, just wrong answers.

## Decision

Add `seq INTEGER PRIMARY KEY` as an explicit rowid alias. Demote `id` to
`TEXT NOT NULL UNIQUE`. Point `content_rowid` at `seq`.

`seq` is internal. It never appears in the public identifier scheme
(section 11), in API responses, or in MCP output.

Maintain the FTS index with triggers rather than application code:
triggers cannot be bypassed by a write path that forgot to call the
updater, and they are inherently transactional with the write.

## Consequences

- `VACUUM` becomes safe, though still discouraged.
- One extra 8-byte column per message.
- Any code path inserting into `messages` gets FTS maintenance free.
