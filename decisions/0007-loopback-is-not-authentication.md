# 0007 — The local daemon requires a bearer token

**Status:** accepted · **Date:** 2026-08-24

## Context

v0.1 section 23 specified `127.0.0.1:8787` and "no public network
listener," implying that loopback binding is the security boundary.

It is not. Every process on the machine can reach loopback — including a
compromised transitive dependency in an unrelated project, a malicious
`postinstall` script, or any browser page that can be induced to issue a
cross-origin request. An unauthenticated listener here is a complete
mail-history exfiltration endpoint reachable by one `curl`.

## Decision

On first start the daemon generates a random 32-byte token at
`server.token_file`, mode `0600`. Every endpoint except `/health`
requires `Authorization: Bearer <token>`. CLI, Python client, and MCP
server read the same file.

Reject requests carrying an `Origin` header outright — no browser has
business calling this API.

## Consequences

- Roughly twenty lines of middleware.
- Token file becomes part of the backup/permissions story; the daemon
  refuses to start if its mode is not `0600`.
