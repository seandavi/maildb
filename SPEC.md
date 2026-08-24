# `maildb` — Build Specification

**Status:** Ready for implementation
**Version:** v0.2 — amended during planning; rationale in `decisions/`
**Primary target:** macOS laptop
**Initial providers:** Gmail, Apple Mail-backed Microsoft 365
**Primary interface:** constrained SQL database connector
**Secondary interface:** explicit email operations
**Agent interface:** MCP, with code-mode compatibility
**Storage:** SQLite + FTS5
**Remote replication:** deferred

---

## 1. Objective

Build a local-first email data service that synchronizes multiple email accounts into a normalized SQLite database and exposes that database through a safe SQL interface suitable for:

* Claude Code / coding agents
* MCP clients
* n8n
* custom agents
* Python/R/CLI clients
* future remote synchronization

The system must hide provider-specific implementation details from consumers.

The core abstraction is:

> **Email accounts are data sources; SQLite is the local indexed representation; SQL is the primary read interface.**

---

# 2. Non-goals for v1

Do **not** build these initially:

* Microsoft Graph integration
* D1 synchronization
* Postgres
* LanceDB/vector search
* embeddings
* calendar
* contacts
* automatic email classification
* autonomous email sending
* web UI
* multi-user authentication
* cloud deployment

The architecture should permit these later.

---

# 3. High-level architecture

```text
                         ┌─────────────────┐
                         │   Gmail API     │
                         └────────┬────────┘
                                  │
                         Gmail Provider
                                  │
                                  │
                         ┌────────▼────────┐
                         │                 │
                         │   Sync Engine   │
                         │                 │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────┐
                         │                 │
                         │ SQLite + FTS5   │
                         │                 │
                         └────────┬────────┘
                                  │
                   ┌──────────────┼──────────────┐
                   │              │              │
                   ▼              ▼              ▼
              SQL Connector    REST API        CLI
                   │
                   ▼
                  MCP
                   │
          ┌────────┴────────┐
          ▼                 ▼
       Agents              n8n


Apple Mail / Microsoft 365
          │
       osascript
          │
   AppleMail Provider
          │
          └──────────────→ Sync Engine
```

---

# 4. Repository

Use Python and `uv`.

```text
maildb/
├── pyproject.toml
├── README.md
├── LICENSE
│
├── src/
│   └── maildb/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       │
│       ├── domain/
│       │   ├── models.py
│       │   ├── enums.py
│       │   └── errors.py
│       │
│       ├── db/
│       │   ├── connection.py
│       │   ├── schema.py
│       │   ├── migrations.py
│       │   ├── queries.py
│       │   └── search.py
│       │
│       ├── providers/
│       │   ├── base.py
│       │   ├── gmail.py
│       │   └── apple_mail.py
│       │
│       ├── sync/
│       │   ├── engine.py
│       │   ├── state.py
│       │   └── reconcile.py
│       │
│       ├── service/
│       │   ├── query.py
│       │   ├── messages.py
│       │   ├── attachments.py
│       │   └── compose.py
│       │
│       ├── api/
│       │   └── app.py
│       │
│       └── mcp/
│           └── server.py
│
├── scripts/
│   └── apple_mail/
│       ├── list_mailboxes.scpt
│       ├── list_messages.scpt
│       ├── get_message.scpt
│       ├── get_attachment.scpt
│       └── send_message.scpt
│
├── migrations/
│
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/
```

---

# 4.1 Dependencies — buy, don't build

**Standing rule: prefer a maintained third-party library over a
hand-rolled client.** Every protocol this project touches — OAuth token
refresh, MIME parsing, RFC-2047 header decoding, the MCP wire format —
is a place where a bespoke implementation becomes permanent maintenance
for no differentiated value. The differentiated value here is the
*schema*, the *safety layer*, and the *provider normalization*. Nothing
else.

| Concern | Library | Why not hand-rolled |
| --- | --- | --- |
| Gmail API + OAuth | `google-api-python-client`, `google-auth`, `google-auth-oauthlib` | Official. Token refresh, DWD impersonation, batch requests, and retry semantics are exactly the upkeep to avoid. |
| Secret storage | `keyring` | macOS Keychain backend; avoids shelling out to `security`. |
| MIME / headers | stdlib `email` + `email.policy.default` | RFC-2047 decoding, address parsing, and message building are solved. Never regex a header. |
| HTML → text | `html2text` | Body-text synthesis when a message has no plain part. |
| SQL validation | `sqlglot` | Real parser. See decisions/0002. |
| Migrations | `yoyo-migrations` | Raw-SQL migrations without dragging in an ORM. Alembic assumes SQLAlchemy. |
| Models / config | `pydantic` v2, `pydantic-settings`, stdlib `tomllib` | |
| CLI | `typer` | |
| REST API | `fastapi` + `uvicorn` | |
| MCP server | official `mcp` Python SDK (FastMCP) | The protocol will change under us; let the SDK absorb it. |
| Logging | `structlog` | Structured JSON out of the box (section 24). |
| Retry / backoff | `tenacity` | Gmail 429/`rateLimitExceeded` handling. |
| Rate limiting | `aiolimiter` | Token bucket for the 250 units/user/second quota. |
| Identifiers | `python-ulid` | Section 11. |
| HTTP client | `httpx` | |
| Tests | `pytest`, `pytest-asyncio`, `vcrpy` | `vcrpy` records real Gmail responses once, replays them forever as fixtures. |

## Deliberate exceptions

Three places where the rule does **not** apply, and why:

**AppleScript execution — `subprocess` + `osascript`, not `appscript`.**
`appscript` is the richer binding, but it has a long history of breaking
across macOS and Python releases and its author has recommended against
new use. Section 8 already mandates `osascript`. Shelling out is the
stable choice here.

**Thread reconstruction — implemented in-repo.** `jwzthreading` exists
but is effectively unmaintained. The union-find over `References` edges
is roughly fifty lines and is core domain logic, not plumbing.

**Async.** The house preference is async for I/O, but
`google-api-python-client` is synchronous and has no maintained async
equivalent (`aiogoogle` is thinner and less proven, and would put OAuth
refresh back in our hands — precisely the upkeep we are avoiding). The
resolution: keep provider adapters synchronous and call them via
`asyncio.to_thread` from the async service layer. The seam stays async;
the vendored client stays boring. Apple Mail is subprocess-bound and
gets the same treatment.

---

# 5. Configuration

Configuration via environment variables and a local TOML/YAML file.

Example:

```toml
[database]
path = "~/.local/share/maildb/mail.db"

[sync]
interval_seconds = 300
# Retaining HTML bodies roughly doubles-to-triples database size and none
# of it is full-text indexed. Off by default. See decisions/0008.
retain_html = false

[server]
bind = "127.0.0.1"
port = 8787
# Path to the 0600 bearer token file. Loopback is not an auth boundary;
# every local process can reach this port. See decisions/0007.
token_file = "~/.local/share/maildb/token"

[accounts.gmail]
provider = "gmail"
email = "..."
enabled = true

[accounts.cu]
provider = "apple_mail"
email = "..."
enabled = true
```

Do **not** put OAuth secrets in this file.

---

# 6. Provider abstraction

Every provider implements the same interface.

```python
class EmailProvider(Protocol):

    def account(self) -> Account:
        ...

    def capabilities(self) -> set[Capability]:
        """What this provider can actually do.

        The schema is built to Gmail's fidelity; Apple Mail cannot fill
        much of it. Providers declare their limits rather than silently
        leaving columns NULL. See section 9.1.
        """
        ...

    def list_mailboxes(self) -> list[Mailbox]:
        ...

    def sync(
        self,
        cursor: SyncCursor | None,
    ) -> SyncResult:
        ...

    def get_message(
        self,
        provider_message_id: str,
    ) -> Message:
        ...

    def get_attachment(
        self,
        provider_message_id: str,
        provider_attachment_id: str,
    ) -> bytes:
        ...

    def create_draft(
        self,
        draft: Draft,
    ) -> Message:
        ...

    def send(
        self,
        message: OutgoingMessage,
    ) -> SendResult:
        ...
```

The application layer must never directly import Gmail or Apple Mail classes.

---

# 7. Provider 1: Gmail — the reference provider

Gmail is not one provider of two equals. It is the **reference
implementation**, and the canonical schema is built to its fidelity.
Apple Mail (section 8) exists only to route around the Microsoft 365
authentication barrier and will leave much of that schema NULL.

Design the schema for what Gmail can express. Do not reduce it to the
intersection of the two providers.

## 7.1 Authentication

Two mechanisms, and the choice is **not** a preference — it is decided
by what kind of account it is.

| | OAuth installed app | Service account + DWD |
| --- | --- | --- |
| Works for `@gmail.com` | yes | **no** |
| Works for Workspace domain | yes | yes, if you are a domain admin |
| Requires admin console | no | yes — authorize the client ID with scopes |
| User consent screen | once, at first run | never |
| Credential | refresh token | JSON key + `subject` impersonation |
| Unattended daemon | yes, once granted | yes |

Domain-wide delegation requires authorizing the service account's client
ID in a Google Workspace admin console. A personal `@gmail.com` account
has no such console, so **a service account cannot access it at all** —
no amount of key configuration changes this. Personal accounts use the
installed-app flow.

Implement both behind one `GmailAuth` seam:

```python
[accounts.gmail-personal]
provider     = "gmail"
email        = "..."
auth_method  = "oauth_installed"     # personal @gmail.com
enabled      = true

[accounts.some-workspace]
provider          = "gmail"
email             = "..."
auth_method       = "service_account"
service_account_file = "~/.config/maildb/sa.json"   # path only, never the key
delegated_subject = "..."            # impersonation target
enabled           = true
```

## 7.1.1 Where the credentials live

An existing GCP project, **`cdsci-infra`**, holds secrets in Google
Secret Manager and carries a configured OAuth consent screen. Secrets are
split by lifecycle (decisions/0012):

| Secret | Store | Why |
| --- | --- | --- |
| OAuth client id + secret | Secret Manager (`cdsci-infra`) | Shared configuration, identical on every host, rotated centrally |
| Per-account refresh token | macOS Keychain | Minted per device, useless elsewhere, read on every refresh — must not depend on a GCP round-trip |
| Service-account key file | Disk, mode `0600` | Referenced by path; the key is never in config |

```toml
[secrets]
backend       = "gsm"
gcp_project   = "cdsci-infra"
token_backend = "keyring"
```

Reading from Secret Manager needs application-default credentials
(`gcloud auth application-default login`). This is a **different Google
identity** from the Gmail OAuth grant; conflating the two is the likely
first support question.

The config file holds references only. The loader matches values against
known credential shapes and refuses to start when one appears, so a
pasted token fails at load rather than at `git push`.

## 7.1.2 Redirect URI

The client is a **Desktop app** OAuth client, which Google permits to use
any loopback port. `InstalledAppFlow.run_local_server(port=0)` therefore
needs no per-port registration: it binds an ephemeral port, receives the
callback, and exits.

If the client is instead registered as a **Web application**, loopback is
not implicit — the exact redirect URI must be registered, and the flow
must pin a fixed port (`server.oauth_redirect_port`, default `8788`).
Check the client type before assuming the ephemeral-port path works.

One consent-screen property to confirm before M3: an app in **Testing**
mode issues refresh tokens that expire after **7 days**. Publishing the
app — unverified and single-user is fine — is what avoids weekly
re-authentication.

## 7.2 Scopes — least privilege, escalated by phase

| Phase | Scope | Grants |
| --- | --- | --- |
| 2 (sync) | `gmail.readonly` | read messages, labels, attachments |
| 6 (writes) | `gmail.modify` | + label changes, archive, mark read, trash |
| 6 (send) | `gmail.send` | + send |
| 6 (drafts) | `gmail.compose` | + create/update drafts |

Never request `https://mail.google.com/` — it grants full IMAP-level
control including permanent deletion, and nothing in this system needs
it. Avoid `gmail.metadata`: it forbids body retrieval, which defeats the
purpose.

Start at `gmail.readonly` and force re-consent when phase 6 lands. A
read-only backfill that later turns out to have held write scope the
whole time is a needless liability.

## 7.3 Synchronization mechanics

* **Backfill:** `users.messages.list` paginated, then `users.messages.get`
  with `format=full`. Batch `get` calls (100 per HTTP batch).
* **Incremental:** `users.history.list` from the stored `historyId`.
  Handle `404` — an expired `historyId` (Gmail retains roughly a week)
  means falling back to a full reconciliation sweep, not crashing.
* **Quota:** 250 units/user/second; `messages.get` costs 5 units.
  A 50k-message backfill is quota-bound, not network-bound. Implement
  token-bucket throttling and exponential backoff on `429`/`403
  rateLimitExceeded` from the first commit, not after the first ban.
* **Authoritative timestamp:** `internalDate`, not the `Date:` header —
  the header is client-supplied and frequently wrong or forged.

Gmail-specific identifiers are retained in dedicated columns and in
`provider_metadata`; they are never the public identifier (section 11).

---

# 8. Provider 2: Apple Mail

The Apple Mail provider exists specifically to support the user's Microsoft 365 account without requiring a new Microsoft Graph application.

Architecture:

```text
Microsoft 365
     │
 Apple Mail
     │
 AppleScript
     │
 AppleMailProvider
```

The provider must use `osascript`/AppleScript rather than reading Apple's private database directly.

**Apple Mail's database is not an API.**

The provider normalizes Apple Mail's output into the canonical model —
and leaves everything it cannot observe as NULL, declared honestly
through `capabilities()` rather than guessed at.

This provider is a **stopgap**, and should be labelled as one in the
code. If Microsoft Graph access ever becomes obtainable, a `graph.py`
provider replaces it wholesale with no change above the provider seam.
Nothing else in the system should be shaped around Apple Mail's limits.

Practical constraints that shape the implementation:

* AppleScript property access is slow — tens of milliseconds per read.
  Every script must return many delimited records per invocation, never
  one message per `osascript` call.
* All message-property access must occur **inside** the
  `tell application "Mail"` block. A handler defined outside it runs
  without Mail's terminology and silently returns empty values rather
  than erroring.
* `provider_message_id` is the RFC-822 `Message-ID` header, falling back
  to a SHA-256 of (`date received`, `sender`, `subject`) when absent.
  Apple Mail exposes no stable internal identifier.
* Full headers are available via `all headers of message`, which is what
  makes `message_headers` populatable for this provider.

---

# 9. Canonical database schema

## `accounts`

```sql
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT,

    -- 'oauth_installed' | 'service_account' | 'none' (apple_mail)
    auth_method TEXT,
    -- JSON array of scopes actually granted, so the service can refuse a
    -- write it lacks scope for instead of failing at the API boundary.
    oauth_scopes TEXT,
    -- Keychain item name or key-file path. NEVER a credential itself.
    credential_ref TEXT,
    -- Service-account impersonation target (Workspace domains only).
    delegated_subject TEXT,

    enabled INTEGER NOT NULL DEFAULT 1,
    sync_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

A `CHECK` constraint is not sufficient to catch a secret pasted into
`credential_ref`; the loader asserts the value does not look like a
token or private key and refuses to start if it does.

---

## 9.1 `provider_capabilities`

The schema is a **superset**, built to Gmail. Apple Mail fills perhaps
60% of it. Without an explicit record of that, an agent writing
`WHERE is_important = 1` gets a silently wrong answer for the Microsoft
365 account — the query is not an error, it just returns nothing,
forever.

```sql
CREATE TABLE provider_capabilities (
    provider   TEXT NOT NULL,
    capability TEXT NOT NULL,
    supported  INTEGER NOT NULL,
    notes      TEXT,

    PRIMARY KEY (provider, capability)
);
```

Seeded at migration time, surfaced in `describe_table` output, and
documented in the schema metadata for every column it governs:

| capability | gmail | apple_mail |
| --- | --- | --- |
| `incremental_history` | yes | no — high-water-mark scan only |
| `deletion_events` | yes | no — reconciliation sweep only |
| `flag_change_events` | yes | no — reconciliation sweep only |
| `labels_many_to_many` | yes | no — one folder per message |
| `native_thread_id` | yes | no — reconstructed from RFC-822 headers |
| `importance_flag` | yes | no |
| `spam_trash_state` | yes | partial — junk mailbox only |
| `size_estimate` | yes | no |
| `raw_headers` | yes | yes |
| `attachment_by_id` | yes | partial — by name and index |
| `server_side_search` | yes | no |
| `drafts` | yes | limited |
| `send` | yes | yes |
| `modify_labels` | yes | move only |

**Rule:** any column that is not universally populated must name its
governing capability in the schema metadata (section 16). This is the
mechanism that stops the superset schema from quietly lying to agents.

---

## `mailboxes`

```sql
CREATE TABLE mailboxes (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    provider_mailbox_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mailbox_type TEXT,          -- inbox|sent|drafts|trash|spam|archive|user
    parent_id TEXT,

    -- Gmail label fidelity. NULL for apple_mail.
    label_type TEXT,            -- 'system' | 'user'
    message_list_visibility TEXT,
    label_list_visibility TEXT,
    messages_total INTEGER,
    messages_unread INTEGER,
    threads_total INTEGER,
    threads_unread INTEGER,
    color_background TEXT,
    color_text TEXT,

    provider_metadata TEXT,
    updated_at TEXT NOT NULL,

    UNIQUE(account_id, provider_mailbox_id)
);
```

---

## `threads`

```sql
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    provider_thread_id TEXT,
    provider_history_id TEXT,
    -- 'provider' when the provider supplied a thread id, 'reconstructed'
    -- when derived from References/In-Reply-To. Agents doing thread
    -- analysis need to know which they are looking at.
    thread_source TEXT,
    subject TEXT,
    first_message_at TEXT,
    last_message_at TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    participants TEXT,
    snippet TEXT,
    updated_at TEXT NOT NULL
);
```

---

## `messages`

```sql
CREATE TABLE messages (
    -- Integer surrogate key. Exists solely so message_fts has a STABLE
    -- content_rowid: an implicit rowid can be renumbered by VACUUM, which
    -- would silently decouple the FTS index from its rows. Never exposed
    -- outside the database. See decisions/0001.
    seq INTEGER PRIMARY KEY,

    -- Public stable identifier (ULID / UUIDv7). See section 11.
    id TEXT NOT NULL UNIQUE,

    account_id TEXT NOT NULL,
    thread_id TEXT,

    provider_message_id TEXT NOT NULL,
    provider_thread_id TEXT,

    -- RFC-822 threading headers. Required to reconstruct threads for any
    -- provider that exposes no thread identifier of its own -- notably
    -- Apple Mail. See decisions/0004.
    message_id_header TEXT,
    in_reply_to TEXT,
    references_header TEXT,

    -- Canonical folder for single-folder providers (Exchange via Apple
    -- Mail). NULL for label-based providers. `message_labels` is
    -- authoritative for mailbox membership. See decisions/0003.
    mailbox_id TEXT,

    subject TEXT,

    sender_name TEXT,
    sender_email TEXT,

    recipients TEXT,
    cc TEXT,
    bcc TEXT,
    reply_to TEXT,

    -- Every timestamp in this database is ISO-8601 UTC, fixed width, with
    -- a trailing Z (YYYY-MM-DDTHH:MM:SSZ), so lexicographic comparison is
    -- chronological comparison. Normalized on write, never on read: agent
    -- SQL will say `received_at >= '2026-01-01'` and must be correct.
    -- See decisions/0006.
    sent_at TEXT,
    received_at TEXT,

    body_text TEXT,
    body_html TEXT,          -- NULL unless sync.retain_html = true
    snippet TEXT,

    has_attachments INTEGER NOT NULL DEFAULT 0,

    -- Gmail's authoritative ordering timestamp. The `Date:` header is
    -- client-supplied and routinely wrong; internal_date is not.
    internal_date TEXT,
    -- Per-message watermark, for resuming history.list mid-page.
    history_id TEXT,
    size_estimate INTEGER,

    is_read INTEGER,
    is_starred INTEGER,
    is_draft INTEGER,
    is_sent INTEGER,
    is_important INTEGER,       -- gmail: importance_flag capability
    is_spam INTEGER,
    is_trash INTEGER,
    is_answered INTEGER,        -- IMAP \Answered; both providers

    -- Bulk-mail signals, lifted from headers at sync time. These make
    -- newsletter/automation triage a WHERE clause instead of a judgment
    -- call, which is the single highest-value query this database serves.
    list_id TEXT,               -- List-Id
    list_unsubscribe TEXT,      -- List-Unsubscribe
    precedence TEXT,            -- Precedence: bulk|list|junk
    auto_submitted TEXT,        -- Auto-Submitted: != "no" means automated

    provider_metadata TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,

    UNIQUE(account_id, provider_message_id)
);

-- Agent-generated SQL filters on these columns almost every time.
CREATE INDEX idx_messages_account_received ON messages(account_id, received_at DESC);
CREATE INDEX idx_messages_received         ON messages(received_at DESC);
CREATE INDEX idx_messages_sender           ON messages(sender_email);
CREATE INDEX idx_messages_thread           ON messages(thread_id);
CREATE INDEX idx_messages_msgid_header     ON messages(message_id_header);
```

---

## `message_labels`

```sql
CREATE TABLE message_labels (
    message_id TEXT NOT NULL,
    mailbox_id TEXT NOT NULL,

    PRIMARY KEY(message_id, mailbox_id)
);
```

This is important because Gmail labels and Exchange folders don't map perfectly.

For providers without `labels_many_to_many`, exactly one row exists per
message, mirroring `messages.mailbox_id`. Consumers should join through
this table unconditionally; it is correct for both shapes.

---

## `message_headers`

Full header fidelity. Gmail returns every header with `format=full`;
Apple Mail exposes them via `all headers of message`. Both providers can
populate this, and it is what makes the extracted columns above
auditable — and future extractions possible without a re-sync.

```sql
CREATE TABLE message_headers (
    message_id TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,   -- preserves header order and duplicates
    name       TEXT NOT NULL,      -- as received, original case
    name_lower TEXT NOT NULL,      -- lookup key
    value      TEXT,

    PRIMARY KEY (message_id, ordinal)
);

CREATE INDEX idx_message_headers_lookup ON message_headers(name_lower, message_id);
```

Headers repeat legitimately (`Received`, `References`), so the key is
(`message_id`, `ordinal`) rather than (`message_id`, `name`).

---

## `drafts`

Gmail drafts carry their own identifier, distinct from the identifier of
the message they contain, and that identity is required to update a
draft rather than accumulate duplicates. Section 19's write operations
depend on this table.

```sql
CREATE TABLE drafts (
    id TEXT PRIMARY KEY,

    account_id TEXT NOT NULL,
    provider_draft_id TEXT,

    message_id TEXT,                -- the draft body, as a messages row
    in_reply_to_message_id TEXT,    -- set for replies
    thread_id TEXT,

    -- 'local'  = composed here, not yet pushed to the provider
    -- 'synced' = exists at the provider
    -- 'sent'   = dispatched
    -- 'discarded'
    status TEXT NOT NULL,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,

    UNIQUE(account_id, provider_draft_id)
);
```

---

## `attachments`

```sql
CREATE TABLE attachments (
    id TEXT PRIMARY KEY,

    message_id TEXT NOT NULL,

    provider_attachment_id TEXT,
    part_id TEXT,               -- Gmail payload partId
    content_id TEXT,            -- Content-ID, for inline images
    is_inline INTEGER NOT NULL DEFAULT 0,

    filename TEXT,
    mime_type TEXT,
    size_bytes INTEGER,

    content_hash TEXT,

    local_path TEXT,
    downloaded INTEGER NOT NULL DEFAULT 0,

    provider_metadata TEXT,

    created_at TEXT NOT NULL
);
```

Attachments are **metadata-only until explicitly retrieved**.

**Consciously deferred:** the full MIME part tree. Gmail's `payload`
exposes a complete nested part structure; v1 stores only leaf parts that
are attachments or inline objects. `message_headers` plus `body_text` /
`body_html` covers every query anticipated for v1. Should a
`message_parts` table become necessary, it can be backfilled from
retained `provider_metadata` without re-syncing.

---

## `sync_state`

```sql
CREATE TABLE sync_state (
    account_id TEXT PRIMARY KEY,

    provider_cursor TEXT,

    last_sync_started TEXT,
    last_sync_completed TEXT,

    messages_seen INTEGER DEFAULT 0,
    messages_added INTEGER DEFAULT 0,
    messages_updated INTEGER DEFAULT 0,
    messages_deleted INTEGER DEFAULT 0,

    status TEXT,
    error TEXT
);
```

---

## `audit_log`

```sql
CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,

    timestamp TEXT NOT NULL,

    account_id TEXT,

    actor TEXT,

    operation TEXT,

    resource_type TEXT,
    resource_id TEXT,

    request TEXT,
    result TEXT
);
```

---

# 10. Full-text search

Use SQLite FTS5.

```sql
CREATE VIRTUAL TABLE message_fts USING fts5(
    subject,
    sender_email,
    recipients,
    body_text,
    content='messages',
    content_rowid='seq'
);
```

The index is maintained by triggers, not by application code. Triggers
cannot be bypassed by a code path that forgets to call the updater, and
they are automatically transactional with the write that fired them:

```sql
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO message_fts(rowid, subject, sender_email, recipients, body_text)
    VALUES (new.seq, new.subject, new.sender_email, new.recipients, new.body_text);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO message_fts(message_fts, rowid, subject, sender_email, recipients, body_text)
    VALUES ('delete', old.seq, old.subject, old.sender_email, old.recipients, old.body_text);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO message_fts(message_fts, rowid, subject, sender_email, recipients, body_text)
    VALUES ('delete', old.seq, old.subject, old.sender_email, old.recipients, old.body_text);
    INSERT INTO message_fts(rowid, subject, sender_email, recipients, body_text)
    VALUES (new.seq, new.subject, new.sender_email, new.recipients, new.body_text);
END;
```

Only `body_text` is indexed. `body_html` is never searched.

`VACUUM` is prohibited on this database while the external-content
mapping is live; run `INSERT INTO message_fts(message_fts) VALUES
('rebuild')` after any operation that could disturb it.

Search should support:

```text
keyword
phrase
AND
OR
NOT
```

and application-level filters:

```text
account
date
sender
recipient
mailbox
has_attachment
read/unread
```

---

# 11. Stable identifiers

Never expose provider IDs as primary identifiers.

Use:

```text
email://{account_id}/{message_id}
thread://{account_id}/{thread_id}
attachment://{account_id}/{attachment_id}
```

Internally use UUIDv7 or ULID.

Provider identifiers are retained separately for synchronization.

---

# 12. Synchronization model

The sync engine runs independently of the API.

```text
sync account
    │
    ├── discover changes
    │
    ├── normalize
    │
    ├── transaction
    │     ├── messages
    │     ├── threads
    │     ├── mailboxes
    │     └── attachments
    │
    └── update sync_state
```

Requirements:

* idempotent
* restartable
* incremental where possible
* safe after interruption
* provider-specific cursors
* periodic reconciliation

A failed sync must not corrupt the existing index.

## 12.1 Incremental strategy per provider

Providers differ fundamentally in what they can tell you, and the sync
engine must not pretend otherwise.

| | Gmail | Apple Mail |
| --- | --- | --- |
| Change feed | `history.list` from `historyId` | none |
| Cursor | `historyId` | per-mailbox high-water-mark on `date received` |
| Deletions | reported explicitly | invisible; reconciliation only |
| Flag changes | reported explicitly | invisible; reconciliation only |
| Cost per poll | one API call | O(mailbox) AppleScript scan |

Apple Mail has no change feed of any kind. A high-water-mark scan finds
new arrivals and nothing else — deletions, moves, and read/unread
changes are structurally invisible to it.

## 12.2 Reconciliation sweep

Reconciliation is therefore a required subsystem, not an optimization.

```text
for each mailbox:
    fetch (provider_message_id, is_read, is_starred) for all messages
    compare against the local index
        present locally, absent remotely  -> set deleted_at
        flags differ                      -> update
        absent locally, present remotely  -> queue for full fetch
```

The sweep fetches identifiers and flags only, never bodies, which keeps
it cheap enough to run on a slow cadence (default: hourly for Apple
Mail, daily for Gmail). Deletions are soft — `deleted_at` is set, rows
are never removed — so a reconciliation bug degrades into stale rows
rather than data loss.

---

# 13. Search API

The canonical read operation is:

```http
POST /v1/query
```

Request:

```json
{
  "sql": "SELECT subject, sender_email, received_at FROM messages WHERE account_id = 'cu' LIMIT 20"
}
```

Response:

```json
{
  "columns": [
    "subject",
    "sender_email",
    "received_at"
  ],
  "rows": [
    ["...", "...", "..."]
  ],
  "row_count": 20,
  "elapsed_ms": 3
}
```

However, `query` is not unrestricted SQL.

---

# 14. SQL safety layer

This is the highest-risk component in the system: it accepts
model-generated SQL and runs it against every message the user has ever
received. It gets three independent layers, and **none of them is a
keyword blocklist**. String matching on `DROP` loses to comments,
unicode escapes, nested CTEs, and whitespace tricks; it is a source of
false confidence, not safety.

### Layer 1 — read-only connection (the real boundary)

```python
conn = sqlite3.connect("file:mail.db?mode=ro", uri=True)
conn.enable_load_extension(False)
```

`mode=ro` is enforced by SQLite itself, below any SQL the model can
write. Even a total failure of layers 2 and 3 cannot produce a write.

### Layer 2 — authorizer callback (capability allowlist)

```python
conn.set_authorizer(authorizer)
```

The callback denies every action code except `SQLITE_SELECT`,
`SQLITE_READ`, and `SQLITE_FUNCTION`, and denies `SQLITE_READ` for any
table outside the published allowlist. This is what actually blocks
`ATTACH`, `PRAGMA`, extension loading, and virtual-table creation — by
capability, not by spelling.

`audit_log` is excluded from the read allowlist: a read-only agent
should not be able to inspect the record of write operations.

### Layer 3 — parse and assert shape (better errors, not security)

Parse with `sqlglot`; require exactly one top-level statement and an AST
root of `Select` (or `With` whose body is a `Select`). This exists to
return *"only SELECT is permitted"* instead of an opaque authorizer
denial. It is a usability layer. Do not let it become the security
story.

### Resource limits

```text
max_rows = 1000
max_execution_time = 5 seconds
max_response_size = 1 MB
```

Row cap is applied by wrapping the query, not by trusting a `LIMIT` in
the submitted SQL. The time limit is enforced with
`conn.set_progress_handler`, which can interrupt a runaway query mid
execution — a wall-clock timeout around `cursor.execute()` cannot.

All three are configurable.

### Non-negotiable test

Every item in section 25's security list must be asserted **with the
authorizer as the only active defense** (parser disabled). If a test
passes only because the parser rejected the string, it is not testing
what protects the database.

---

# 15. Database connector interface

The primary MCP interface is intentionally tiny:

```text
list_tables()
describe_table(table)
execute_query(sql)
```

### `list_tables`

Returns:

```json
{
  "tables": [
    {
      "name": "messages",
      "description": "Normalized email messages"
    },
    {
      "name": "threads",
      "description": "Conversation threads"
    }
  ]
}
```

### `describe_table`

Returns columns plus semantic descriptions.

### `execute_query`

Accepts a single read-only SQL statement.

---

# 16. Schema metadata

Maintain metadata separately from SQLite's raw schema.

For example:

```yaml
messages:
  description: Normalized email messages across all accounts
  columns:
    account_id:
      description: Internal account identifier
    received_at:
      description: Provider-reported received timestamp
    body_text:
      description: Normalized plain-text message body
    provider_message_id:
      description: Provider-specific message identifier
```

This is critical for good agent SQL generation.

---

# 17. MCP

MCP should expose:

```text
list_tables
describe_table
execute_query
```

plus a very small number of non-SQL operations.

### Required non-SQL tools

```text
get_attachment
download_attachment
```

Potentially:

```text
create_draft
send_message
```

but these should not be available to an unrestricted read-only agent.

---

# 18. Code-mode

The backend should provide a Python client:

```python
from maildb import Database

db = Database()

db.list_tables()

db.describe("messages")

rows = db.query("""
    SELECT
        sender_email,
        COUNT(*) AS n
    FROM messages
    WHERE received_at >= '2026-01-01'
    GROUP BY sender_email
    ORDER BY n DESC
    LIMIT 20
""")
```

The client supports two transports over one validation path:

```python
Database()                  # HTTP to the local daemon (default)
Database(local=True)        # opens the SQLite file directly, read-only
```

`local=True` does **not** bypass the safety layer. It constructs the
same read-only connection with the same authorizer from section 14 and
runs the same validator. The security boundary is the authorizer, not
the process boundary — so an in-process reader is exactly as constrained
as an HTTP one, while notebooks, scripts, and ad-hoc analysis keep
working when the daemon is not running. See decisions/0009.

The single shared implementation is what makes this safe. If the two
transports ever diverge into two query paths, this decision is void.

---

# 19. Explicit service API

The SQL layer is for reads.

The domain service provides writes:

```python
mail.create_draft(...)
mail.reply(...)
mail.forward(...)
mail.send(...)
mail.archive(...)
mail.mark_read(...)
```

These operations eventually call:

```text
Domain service
    ↓
Provider adapter
    ↓
Gmail / Apple Mail
```

They must **not mutate SQLite first and hope the provider catches up**.

Provider is authoritative for writes.

---

# 20. Write safety

Default policy:

| Operation            | Agent default             |
| -------------------- | ------------------------- |
| Search               | Allowed                   |
| Read                 | Allowed                   |
| Attachment retrieval | Allowed                   |
| Mark read            | Allowed                   |
| Archive              | Confirmation/configurable |
| Move                 | Confirmation/configurable |
| Delete               | Confirmation              |
| Draft                | Allowed                   |
| Send                 | **Explicit confirmation** |
| Reply                | **Explicit confirmation** |
| Forward              | **Explicit confirmation** |

Every write is recorded in `audit_log`.

---

# 21. REST API

The REST API is secondary to MCP but useful for n8n.

Minimum endpoints:

```text
GET  /health
GET  /v1/accounts
GET  /v1/mailboxes
POST /v1/query

GET  /v1/messages/{id}
GET  /v1/threads/{id}
GET  /v1/messages/{id}/attachments
GET  /v1/attachments/{id}

POST /v1/drafts
POST /v1/messages/{id}/reply
POST /v1/messages/{id}/send

POST /v1/sync/{account_id}
GET  /v1/sync/status
```

---

# 22. CLI

The CLI is useful for administration and debugging.

```bash
maildb accounts
maildb sync
maildb sync gmail-personal
maildb status
maildb tables
maildb describe messages
maildb query 'SELECT ...'
maildb search "NIH R01"
```

The CLI should use the same application service as MCP/API.

---

# 23. Local daemon

Run as a macOS LaunchAgent.

Responsibilities:

```text
maildb daemon
├── API server
├── MCP server
└── sync scheduler
```

Default:

```text
127.0.0.1:8787
```

No public network listener.

### Loopback is not an authentication boundary

Every process on the machine can reach `127.0.0.1:8787`, including a
compromised dependency in an unrelated project. An unauthenticated
listener on this port is a complete mail-history exfiltration endpoint
reachable by one `curl`.

On first start the daemon generates a random token at
`server.token_file` with mode `0600`. Every endpoint except `/health`
requires `Authorization: Bearer <token>`. The CLI, Python client, and
MCP server read the same file. See decisions/0007.

---

# 24. Logging / observability

Use structured JSON logs.

Important events:

```text
sync.started
sync.completed
sync.failed
message.created
message.updated
message.deleted
attachment.downloaded
query.executed
write.requested
write.completed
write.failed
```

Eventually emit OpenTelemetry if useful, but **don't make OTEL a v1 dependency**.

---

# 25. Testing

### Unit

* canonical models
* provider normalization
* SQL validation
* FTS queries
* sync state
* ID generation

### Provider integration

Gmail fixture/test account.

Apple Mail tests using fixture data where possible.

### Database

Test:

* migrations
* idempotent upsert
* deletion
* thread reconstruction
* FTS synchronization

### Security

Explicitly test rejection of:

```sql
DROP TABLE messages
```

```sql
ATTACH DATABASE ...
```

```sql
PRAGMA ...
```

multiple statements, etc.

---

# 26. Initial implementation order

### Phase 1 — Database

1. [ ] Repository scaffold
2. [ ] Pydantic/domain models
3. [ ] SQLite connection management
4. [ ] migrations
5. [ ] canonical schema
6. [ ] FTS5
7. [ ] schema metadata

### Phase 2 — Gmail

8. [ ] Gmail OAuth
9. [ ] mailbox discovery
10. [ ] initial sync
11. [ ] incremental sync
12. [ ] message retrieval
13. [ ] attachment metadata

### Phase 3 — Apple Mail

14. [ ] AppleScript abstraction
15. [ ] mailbox discovery
16. [ ] message discovery
17. [ ] message retrieval
18. [ ] incremental synchronization
19. [ ] attachment retrieval

### Phase 4 — Query service

20. [ ] SQL parser/validator
21. [ ] query execution
22. [ ] row/size/time limits
23. [ ] REST `/query`
24. [ ] CLI

### Phase 5 — MCP

25. [ ] `list_tables`
26. [ ] `describe_table`
27. [ ] `execute_query`
28. [ ] attachment tools
29. [ ] MCP authentication/local transport

### Phase 6 — Writes

30. [ ] drafts
31. [ ] replies
32. [ ] send
33. [ ] audit logging
34. [ ] confirmation mechanism

### Phase 7 — Daemon

35. [ ] background sync
36. [ ] LaunchAgent
37. [ ] health/status endpoints

---

# 27. Future architecture

Once this works, the natural evolution is:

```text
                         Provider layer
                 ┌──────────┼──────────┐
                 │          │          │
               Gmail     AppleMail   future
                 │          │
                 └────┬─────┘
                      ▼
                  Sync Engine
                      │
               ┌──────▼──────┐
               │   SQLite    │
               │    FTS5     │
               └──────┬──────┘
                      │
               ┌──────▼──────┐
               │ SQL Service │
               └──────┬──────┘
                      │
             ┌────────┼─────────┐
             ▼        ▼         ▼
            MCP     Code Mode  REST
             │        │         │
             ▼        ▼         ▼
           Agents   Agents     n8n
```

Later:

```text
SQLite
   │
   ├──→ D1
   │
   ├──→ DuckDB
   │
   └──→ vector index
```

without changing the provider or agent interfaces.

---

## The key architectural decision

This is explicitly **not** an "email MCP server."

The core product is:

> **A local, multi-provider, synchronized email database with a safe SQL interface.**

MCP is merely one way to expose it.

That distinction matters because it gives a natural future extension:

```text
maildb
  ├── email
  ├── calendar
  ├── contacts
  ├── tasks
  └── files
```

where agents can eventually query a coherent personal data store with SQL rather than learning 40 different MCP tools.

For v1, resist that expansion. **Build the email database exceptionally well, make the SQL connector excellent, and keep the provider adapters isolated.**
