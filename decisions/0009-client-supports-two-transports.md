# 0009 — Python client supports HTTP and in-process reads

**Status:** accepted · **Date:** 2026-08-24

## Context

v0.1 section 18 required the Python client to speak to the service API
rather than open SQLite directly, "to preserve the security boundary."

The reasoning conflates two boundaries. What constrains a reader is the
read-only connection and the authorizer (ADR 0002), not the fact that a
socket sits in between. Requiring HTTP means `from maildb import
Database` fails whenever the daemon is not running — real friction for
notebooks and ad-hoc analysis on a single-user laptop.

## Decision

```python
Database()               # HTTP to the local daemon (default)
Database(local=True)     # direct read-only SQLite access
```

Both construct the same validator and the same authorizer-guarded
connection. One implementation, two transports.

## Consequences

- **This is only safe while the two paths share one implementation.** If
  they ever fork into separate query paths, this decision is void and
  the HTTP-only rule returns.
- An architecture test asserts both transports resolve to the same
  execution function.
