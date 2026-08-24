# `maildb` — Build Specification

**Status:** Ready for implementation
**Version:** v0.1
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

# 5. Configuration

Configuration via environment variables and a local TOML/YAML file.

Example:

```toml
[database]
path = "~/.local/share/maildb/mail.db"

[sync]
interval_seconds = 300

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

# 7. Provider 1: Gmail

Use the Gmail API with OAuth.

Responsibilities:

* authentication
* mailbox/label discovery
* incremental message synchronization
* message retrieval
* attachment retrieval
* sending/drafting

Gmail-specific IDs remain in provider metadata.

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

The provider should normalize Apple Mail's output into the canonical model.

---

# 9. Canonical database schema

## `accounts`

```sql
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    sync_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

---

## `mailboxes`

```sql
CREATE TABLE mailboxes (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    provider_mailbox_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mailbox_type TEXT,
    parent_id TEXT,
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
    id TEXT PRIMARY KEY,

    account_id TEXT NOT NULL,
    thread_id TEXT,

    provider_message_id TEXT NOT NULL,
    provider_thread_id TEXT,

    mailbox_id TEXT,

    subject TEXT,

    sender_name TEXT,
    sender_email TEXT,

    recipients TEXT,
    cc TEXT,
    bcc TEXT,
    reply_to TEXT,

    sent_at TEXT,
    received_at TEXT,

    body_text TEXT,
    body_html TEXT,
    snippet TEXT,

    has_attachments INTEGER NOT NULL DEFAULT 0,

    is_read INTEGER,
    is_starred INTEGER,
    is_draft INTEGER,
    is_sent INTEGER,

    provider_metadata TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,

    UNIQUE(account_id, provider_message_id)
);
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

---

## `attachments`

```sql
CREATE TABLE attachments (
    id TEXT PRIMARY KEY,

    message_id TEXT NOT NULL,

    provider_attachment_id TEXT,

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
    content_rowid='rowid'
);
```

The application must maintain the FTS index transactionally with message updates.

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

The SQL connector must enforce:

### Allowed

```text
SELECT
WITH ... SELECT
```

### Forbidden

```text
INSERT
UPDATE
DELETE
DROP
ALTER
CREATE
ATTACH
DETACH
PRAGMA
.load
.read
```

Also reject:

* multiple statements
* filesystem paths
* extension loading
* virtual table creation

Set:

```text
max_rows = 1000
max_execution_time = 5 seconds
max_response_size = 1 MB
```

These should be configurable.

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

This client should speak to the service API rather than open the SQLite file directly.

That preserves the security boundary.

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
