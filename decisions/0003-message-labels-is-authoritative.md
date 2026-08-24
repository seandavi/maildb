# 0003 — `message_labels` is authoritative for mailbox membership

**Status:** accepted · **Date:** 2026-08-24

## Context

v0.1 carried both `messages.mailbox_id` (scalar) and a `message_labels`
junction table. Gmail is many-to-many; Exchange folders are one-to-many.
Two representations of the same fact will diverge.

## Decision

`message_labels` is authoritative and always populated — including for
single-folder providers, which simply get exactly one row.

`messages.mailbox_id` is retained as a denormalized convenience naming
the canonical folder for providers without the `labels_many_to_many`
capability. It is NULL for Gmail. It is never the basis of a correctness
decision.

Consumers join through `message_labels` unconditionally; that is correct
for both provider shapes.

## Consequences

- Sync must write the junction table in the same transaction.
- Schema metadata must state plainly that `mailbox_id` is NULL for
  Gmail, or agents will filter on it and get empty results.
