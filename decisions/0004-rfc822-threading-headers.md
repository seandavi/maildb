# 0004 — Store RFC-822 threading headers

**Status:** accepted · **Date:** 2026-08-24

## Context

v0.1 listed "thread reconstruction" as a test target, but `messages`
held no `Message-ID`, `In-Reply-To`, or `References`. Gmail supplies
`threadId`; Apple Mail supplies no thread identity at all. Without the
headers, threading the Microsoft 365 account is not merely hard, it is
impossible.

## Decision

Store `message_id_header`, `in_reply_to`, `references_header` on
`messages`, indexed on `message_id_header`. Store all headers verbatim
in `message_headers`.

`threads.thread_source` records `'provider'` or `'reconstructed'`, so
consumers know which they are looking at.

Reconstruction: union-find over `References` ∪ `In-Reply-To` edges,
falling back to normalized-subject + participant-set + time-window
heuristics only where headers are absent.

## Consequences

- Apple Mail threads will be imperfect. That is a property of the
  source, and `thread_source` makes it visible rather than hidden.
- Retaining full headers lets new extractions be backfilled without
  re-syncing.
