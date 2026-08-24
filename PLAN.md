# `maildb` — Implementation Plan

Companion to [SPEC.md](SPEC.md). The spec says *what*; this says *in
what order, and how we know each step is done*.

**Reading order:** `SPEC.md` §1–3 for the shape → this file for the
sequence → `decisions/` for why the spec says what it says.

---

## What changed from spec v0.1

Twelve decisions were made during planning. Each has an ADR.

| # | Change | ADR |
| --- | --- | --- |
| 1 | `messages` gains `seq INTEGER PRIMARY KEY`; FTS driven by triggers | [0001](decisions/0001-integer-surrogate-key-for-fts.md) |
| 2 | SQL safety is authorizer-first; keyword blocklist rejected | [0002](decisions/0002-sql-safety-is-authorizer-first.md) |
| 3 | `message_labels` authoritative; `mailbox_id` demoted to a hint | [0003](decisions/0003-message-labels-is-authoritative.md) |
| 4 | RFC-822 threading headers stored; `thread_source` recorded | [0004](decisions/0004-rfc822-threading-headers.md) |
| 5 | Apple Mail incremental sync replaced by high-water-mark + sweep | [0005](decisions/0005-apple-mail-has-no-change-feed.md) |
| 6 | Timestamps mandated fixed-width ISO-8601 UTC | [0006](decisions/0006-iso8601-utc-timestamps.md) |
| 7 | Daemon requires a bearer token; loopback is not auth | [0007](decisions/0007-loopback-is-not-authentication.md) |
| 8 | `body_html` retention opt-in, default off | [0008](decisions/0008-html-bodies-are-opt-in.md) |
| 9 | Python client supports HTTP *and* in-process reads | [0009](decisions/0009-client-supports-two-transports.md) |
| 10 | Gmail is the reference provider; schema is a Gmail-fidelity superset | [0010](decisions/0010-gmail-is-the-reference-provider.md) |
| 11 | Prefer maintained libraries over hand-rolled clients | [0011](decisions/0011-prefer-third-party-clients.md) |
| 12 | Shared secrets in Secret Manager (`cdsci-infra`), refresh tokens in the Keychain | [0012](decisions/0012-secrets-live-in-two-places.md) |

---

## Milestone order

Spec §26 runs `DB → Gmail → AppleMail → query → MCP → writes → daemon`.
This plan reorders to **`DB → query → Gmail → MCP → AppleMail → writes
→ daemon`** for two reasons:

- **The SQL safety layer is the highest-risk component and needs no real
  mail to harden.** It is fully testable against fixtures. Building it
  before any live mailbox exists means the attack surface is closed
  before there is anything behind it worth taking.
- **There is no way to inspect sync output until the query layer
  exists.** Debugging a provider by opening the `.db` by hand is a tax
  paid on every subsequent phase.

Apple Mail moves after MCP because it is the stopgap provider
([0010](decisions/0010-gmail-is-the-reference-provider.md)) and the
slowest to iterate on. Gmail first also closes the known Gmail blind
spot in the current mail-triage workflow, which is the nearest thing
this project has to a user-visible win.

```text
M1  Database        schema, migrations, FTS, metadata
M2  Query service   validator, executor, limits, CLI      <-- moved up
M3  Gmail           OAuth, backfill, incremental sync
M4  MCP             list_tables / describe_table / execute_query
M5  Apple Mail      AppleScript bridge, high-water-mark, sweep
M6  Writes          drafts, reply, send, audit, confirmation
M7  Daemon          scheduler, LaunchAgent, health
```

---

## M1 — Database ✅

**Status: complete.** 148 tests, ruff and mypy-strict clean.

**Goal:** a migrated, empty database that an agent could already read a
schema description from.

- [x] `uv init`, src layout, `pyproject.toml`, ruff + mypy strict, pytest
- [x] Pydantic domain models (`Account`, `Mailbox`, `Thread`, `Message`,
      `Attachment`, `Draft`, `Capability`) and `enums.py` / `errors.py`
- [x] Connection management: WAL, foreign keys on, busy timeout,
      separate read-only and read-write connection factories
- [x] `yoyo-migrations` wired; migration `0001` = full canonical schema
- [x] FTS5 external-content table + the three triggers
- [x] `provider_capabilities` seeded for `gmail` and `apple_mail`
- [x] Schema metadata as YAML, with every non-universal column naming
      its governing capability
- [x] ULID generation; `email://`, `thread://`, `attachment://` URIs
- [x] Fixture generator producing a realistic multi-account corpus
      (threads, labels, attachments, bulk mail) — every later milestone
      tests against this

**Exit criteria**

- `pytest` green; migrations apply and roll back cleanly
- Insert a message → it is immediately findable via `message_fts`
- Update and delete keep FTS consistent (property test over random ops)
- A timestamp violating the ISO-8601 `CHECK` is rejected
- Every column in the schema has a metadata description — asserted by a
  test, so drift is a build failure

**Risk:** low. This is the part that is fully understood.

**Notes from the build.** Two things the design did not anticipate:

* FTS5 reads `PRAGMA data_version` internally on every MATCH, so a
  blanket pragma denial makes full-text search impossible. Exactly one
  pragma is allowed, read-only.
* SQLite fires `SQLITE_READ` for CTE and subquery *aliases* too, using
  the alias as the table name — so "deny unless allowlisted" rejects
  every query containing a CTE. The authorizer now distinguishes names
  that are real tables on disk from names that are not; a CTE shadowing a
  real table name is still denied, which fails closed.

---

## M2 — Query service ✅

**Status: complete.** The security suite runs with the parser disabled
and passes; every rejection it asserts is the authorizer's doing.

**Goal:** the safety boundary, hardened before there is real mail behind
it.

- [x] `sqlglot` validator: one statement, `Select` / `With(Select)` root
- [x] Authorizer callback: allowlist by action code and table;
      `audit_log` excluded
- [x] Read-only URI connection, extensions disabled
- [x] Row cap via `fetchmany` (SQLite streams rows, so this caps results
      without rewriting SQL we would otherwise have to transform correctly
      for every query shape); `set_progress_handler` timeout; response
      size cap
- [x] `search()` helper composing FTS5 MATCH with structured filters
      (account, date, sender, mailbox, has_attachment, read state)
- [x] `typer` CLI: `tables`, `describe`, `query`, `search`, `status`
- [x] Python client, both transports, one shared execution path

**Exit criteria**

- **The security suite passes with the `sqlglot` layer disabled.** This
  is the one non-negotiable test in the project: if a rejection depends
  on the parser, the database is not actually protected
  ([0002](decisions/0002-sql-safety-is-authorizer-first.md)).
- Rejected: `DROP`, `ATTACH`, `PRAGMA`, `INSERT`, stacked statements,
  comment-obfuscated keywords, `CREATE VIRTUAL TABLE`, extension loads,
  and any read of `audit_log`
- A deliberate cartesian join is interrupted by the progress handler,
  not by a wall-clock timer that lets it keep burning CPU
- Architecture test: both client transports resolve to the same
  execution function ([0009](decisions/0009-client-supports-two-transports.md))

**Risk:** medium — and it was the right thing to front-load. Both
surprises above were authorizer behaviour that would have been far more
disruptive to discover with two live mailboxes already synced.

---

## M3 — Gmail

**Goal:** a real mailbox, fully synced, queryable.

- [ ] `GmailAuth` seam: installed-app flow *and* service-account DWD
      (see spec §7.1 — DWD cannot reach personal `@gmail.com` accounts)
- [ ] Refresh token in Keychain via `keyring`; config holds a reference,
      never a secret; loader refuses to start on a secret-shaped value
- [ ] Label discovery → `mailboxes` (including counts, colors, types)
- [ ] Backfill: paginated `messages.list` + batched `messages.get`
- [ ] Normalization: headers → `message_headers`, extracted bulk-mail
      columns, `internalDate` as authoritative, HTML → `body_text`
- [ ] Incremental: `history.list` from `historyId`, with `404` →
      reconciliation fallback
- [ ] Reconciliation sweep (IDs + flags only)
- [ ] Attachment metadata; `attachments` rows, no bytes
- [ ] `tenacity` backoff + `aiolimiter` token bucket from the first
      commit, not after the first rate-limit ban

**Exit criteria**

- Full backfill of a real account completes without manual intervention
- Re-running sync is a no-op: zero inserts, zero updates (idempotency)
- Kill `-9` mid-sync, restart → no duplicates, no lost messages, no
  corrupt FTS
- Deleting a message in the Gmail UI sets `deleted_at` on next sync
- Backfill respects quota under sustained load (assert on the limiter,
  not on wall clock)
- `WHERE list_id IS NOT NULL` cleanly separates bulk from human mail —
  the query this database exists to make trivial

**Risk:** medium. Quota behavior and `historyId` expiry are the parts
that will surprise us.

---

## M4 — MCP

**Goal:** an agent can answer questions about the mailbox without
learning a tool per operation.

- [ ] FastMCP server exposing `list_tables`, `describe_table`,
      `execute_query`
- [ ] `describe_table` emits column semantics **and** provider capability
      caveats, so the model knows which columns are NULL for which account
- [ ] `get_attachment` / `download_attachment`
- [ ] stdio transport; bearer token when over HTTP
- [ ] Read-only tool profile is the default; write tools not registered
      unless explicitly enabled

**Exit criteria**

- An agent given only the three tools answers "what did Wells Messersmith
  send me in the last two weeks that I have not replied to?" in a single
  query, with no schema hints in its prompt
- Same agent, asked something requiring `is_important` on the Apple Mail
  account, correctly reports the limitation rather than returning an
  empty result as though it were an answer

That second criterion is the real test of whether
`provider_capabilities` works.

**Risk:** low-medium. Mostly a question of metadata quality.

---

## M5 — Apple Mail

**Goal:** the Microsoft 365 account, on the same schema, honestly
degraded.

- [ ] `osascript` subprocess bridge: timeouts, error surfacing, batched
      record output (never one invocation per message)
- [ ] `.scpt` files per operation, all property access inside the
      `tell application "Mail"` block
- [ ] Mailbox discovery across all accounts
- [ ] `provider_message_id` = `Message-ID` header, SHA-256 fallback
- [ ] Full headers via `all headers of message` → `message_headers`
- [ ] High-water-mark incremental scan per mailbox
- [ ] Reconciliation sweep (this is how deletions and flag changes are
      detected at all — [0005](decisions/0005-apple-mail-has-no-change-feed.md))
- [ ] Thread reconstruction: union-find over `References` / `In-Reply-To`
- [ ] Attachment retrieval
- [ ] `capabilities()` reporting the real limits

**Exit criteria**

- 10k messages sync in a defensible wall-clock time; if not, the batching
  is wrong
- A message deleted in Mail.app is marked deleted by the next sweep
- Threads reconstruct correctly against a hand-verified fixture;
  `thread_source = 'reconstructed'` on every one
- Automation permission failure produces an actionable error, never
  silent empty output — the known failure mode of this bridge

**Risk:** high, and it is contained here by design. AppleScript is slow,
loosely typed, and fails quietly. Nothing above the provider seam depends
on it.

---

## M6 — Writes

**Goal:** the agent can act, under supervision.

- [ ] Scope escalation to `gmail.modify` / `gmail.send` / `gmail.compose`
- [ ] `create_draft`, `reply`, `forward`, `send`, `archive`, `mark_read`
- [ ] Provider-authoritative ordering: call the provider, then reconcile
      locally — never write SQLite first and hope
- [ ] `audit_log` on every attempt, including failures
- [ ] Confirmation mechanism enforcing the spec §20 policy table
- [ ] Send guarded by explicit confirmation, with the resolved recipient
      list shown before dispatch

**Exit criteria**

- Every operation in §20 has a test asserting its confirmation class
- A provider failure leaves local state unchanged and an `audit_log` row
  recording the failure
- Send cannot be reached by any tool available in the read-only MCP
  profile — asserted, not assumed

**Risk:** medium. The danger is not technical; it is an agent sending
something. Confirmation design matters more than the API calls.

---

## M7 — Daemon

- [ ] Sync scheduler with per-account cadence and jitter
- [ ] FastAPI app; token middleware; `Origin` header rejected outright
- [ ] `/health`, `/v1/sync/status`
- [ ] LaunchAgent plist, install/uninstall via CLI
- [ ] `structlog` JSON to a rotating file

**Exit criteria**

- Survives sleep/wake and network loss without wedging
- Unauthenticated request to any endpoint but `/health` gets `401`
- Sync failure is visible in `/v1/sync/status` and does not stop the
  scheduler

**Risk:** low.

---

## Definition of done for v1

Both accounts sync continuously and unattended. An agent with three MCP
tools answers substantive questions about years of mail. Drafts can be
composed; nothing sends without explicit confirmation. The security
suite passes with the parser disabled.

Explicitly **not** in v1: Graph API, D1, embeddings, calendar, contacts,
web UI, multi-user, cloud. Spec §2 holds.

---

## Open questions

1. ~~**Gmail OAuth client registration.**~~ **Resolved:** the
   `cdsci-infra` GCP project has a configured consent screen, and
   secrets live in Secret Manager (ADR 0012). Two things still to
   confirm before M3: whether the OAuth client is registered as a
   *Desktop app* (any loopback port works) or a *Web application* (a
   fixed redirect port must be registered), and whether the consent
   screen is Published rather than in Testing mode — Testing mode expires
   refresh tokens after 7 days.
2. **Backfill depth.** Full mailbox history, or a bounded window (e.g.
   3 years) with on-demand backfill? Affects M3 duration and database
   size materially. *Decide before M3.*
3. **Attachment storage location and retention.** Content-addressed under
   `~/.local/share/maildb/attachments/`? Any size cap?
4. **Second Gmail account?** The config is multi-account from day one;
   worth knowing whether M3 must handle two from the start.

## Deferred, with a path back

Full MIME part tree · Microsoft Graph provider · D1 replication ·
vector index · calendar and contacts as sibling tables. Each is
reachable without disturbing the provider seam or the agent interface —
which is the entire point of the architecture.
