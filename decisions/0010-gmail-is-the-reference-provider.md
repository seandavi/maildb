# 0010 — Gmail is the reference provider; the schema is a superset

**Status:** accepted · **Date:** 2026-08-24

## Context

v0.1 presented Gmail and Apple Mail as two providers of equal standing.
They are not. Gmail offers a real API: a change feed, labels, thread
identity, importance, size, server-side search, and full headers. Apple
Mail is a scripting bridge adopted for exactly one reason — to route
around the Microsoft 365 authentication barrier without registering a
Graph application.

Designing the schema to the intersection of the two would discard most
of what Gmail can express, permanently, to accommodate a stopgap.

## Decision

The canonical schema is built to **Gmail's fidelity**. Apple Mail fills
what it can and declares the rest through `capabilities()`.

Because a superset schema will otherwise lie by omission — `WHERE
is_important = 1` returns nothing for the Microsoft 365 account, with no
error to indicate why — a `provider_capabilities` table records what
each provider actually supports, is surfaced in `describe_table`, and is
referenced from the schema metadata of every non-universal column.

**Authentication is determined by account type, not preference.**
Service accounts with domain-wide delegation require a Google Workspace
admin console to authorize the client ID. A personal `@gmail.com`
account has none, so a service account cannot reach it at all. Personal
accounts use the OAuth installed-app flow; Workspace accounts may use
either. Both sit behind one `GmailAuth` seam.

## Consequences

- Many columns are NULL for `apple_mail`. This is expected and
  documented, not a defect.
- If Microsoft Graph access becomes available, `graph.py` replaces the
  Apple Mail provider with no change above the provider seam — and fills
  in most of the currently-NULL columns.
- Nothing above the provider layer may be shaped around Apple Mail's
  limitations.
