# 0005 — Apple Mail sync is high-water-mark plus reconciliation

**Status:** accepted · **Date:** 2026-08-24

## Context

v0.1 step 18 called for "incremental synchronization" of Apple Mail in
the same breath as Gmail's. Gmail has `history.list`, which reports
additions, deletions, and flag changes since a cursor. AppleScript has
no equivalent. Nothing in Mail.app's scripting dictionary exposes a
change feed.

A high-water-mark scan on `date received` finds new arrivals and nothing
else. Deletions, moves, and read/unread changes are structurally
invisible to it.

## Decision

Two mechanisms, not one:

- **Incremental:** per-mailbox high-water-mark on `date received`.
  Cheap, frequent, finds new mail.
- **Reconciliation sweep:** fetch (`provider_message_id`, `is_read`,
  `is_starred`) for every message in a mailbox — identifiers and flags
  only, never bodies — and diff against the local index. Missing locally
  → fetch; missing remotely → set `deleted_at`; flags differ → update.

Default cadence: hourly for Apple Mail, daily for Gmail (where the sweep
is a backstop against an expired `historyId`, which Gmail retains only
about a week).

Deletions are always soft. A reconciliation bug degrades into stale rows
rather than data loss.

## Consequences

- Reconciliation is a required subsystem, not an optimization.
- Apple Mail flag state is eventually consistent, bounded by the sweep
  interval. Documented via `provider_capabilities`.
