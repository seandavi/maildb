"""Canonical maildb schema.

Written as a Python migration rather than raw SQL because yoyo splits
``.sql`` files on semicolons, which would shred the FTS triggers (their
bodies contain statement terminators).

Design notes worth reading before changing anything here:

* ``messages.seq`` is an explicit INTEGER PRIMARY KEY so ``message_fts``
  has a stable ``content_rowid``. An implicit rowid can be renumbered by
  VACUUM, silently decoupling the search index from its rows (ADR 0001).
* Every timestamp is fixed-width ISO-8601 UTC, enforced by GLOB rather
  than LIKE because LIKE is case-insensitive and would accept a lowercase
  ``z`` (ADR 0006).
* ``message_labels`` is authoritative for mailbox membership;
  ``messages.mailbox_id`` is a convenience for single-folder providers
  (ADR 0003).
"""

from yoyo import step

__depends__: set[str] = set()

#: Fixed-width ISO-8601 UTC. GLOB is case-sensitive; LIKE is not.
TS_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"


def ts(column: str, *, required: bool = False) -> str:
    """A CHECK constraint asserting the canonical timestamp format."""
    null_clause = "" if required else f"{column} IS NULL OR "
    return f"CHECK ({null_clause}{column} GLOB '{TS_GLOB}')"


steps = [
    step(
        f"""
        CREATE TABLE accounts (
            id            TEXT PRIMARY KEY,
            provider      TEXT NOT NULL CHECK (provider IN ('gmail', 'apple_mail')),
            email         TEXT NOT NULL,
            display_name  TEXT,

            -- Determined by account type, not preference: a service
            -- account cannot reach a personal @gmail.com address.
            auth_method   TEXT CHECK (
                auth_method IN ('oauth_installed', 'service_account', 'none')
            ),
            -- JSON array of scopes actually granted, so the service can
            -- refuse a write it lacks scope for rather than failing at
            -- the API boundary.
            oauth_scopes  TEXT,
            -- Secret Manager name, Keychain entry, or key-file path.
            -- NEVER a credential.
            credential_ref     TEXT,
            delegated_subject  TEXT,

            enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            sync_enabled  INTEGER NOT NULL DEFAULT 1 CHECK (sync_enabled IN (0, 1)),

            created_at    TEXT NOT NULL {ts('created_at', required=True)},
            updated_at    TEXT NOT NULL {ts('updated_at', required=True)}
        )
        """,
        "DROP TABLE accounts",
    ),
    step(
        """
        -- The schema is a superset built to Gmail's fidelity. Without an
        -- explicit record of what each provider supports, a query like
        -- `WHERE is_important = 1` returns nothing for an Apple Mail
        -- account and looks like an answer rather than a limitation.
        CREATE TABLE provider_capabilities (
            provider    TEXT NOT NULL,
            capability  TEXT NOT NULL,
            supported   INTEGER NOT NULL CHECK (supported IN (0, 1)),
            notes       TEXT,

            PRIMARY KEY (provider, capability)
        )
        """,
        "DROP TABLE provider_capabilities",
    ),
    step(
        f"""
        CREATE TABLE mailboxes (
            id                   TEXT PRIMARY KEY,
            account_id           TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            provider_mailbox_id  TEXT NOT NULL,
            name                 TEXT NOT NULL,
            mailbox_type         TEXT,
            parent_id            TEXT REFERENCES mailboxes(id) ON DELETE SET NULL,

            -- Gmail label fidelity; NULL for apple_mail.
            label_type               TEXT,
            message_list_visibility  TEXT,
            label_list_visibility    TEXT,
            messages_total           INTEGER,
            messages_unread          INTEGER,
            threads_total            INTEGER,
            threads_unread           INTEGER,
            color_background         TEXT,
            color_text               TEXT,

            provider_metadata    TEXT,
            updated_at           TEXT NOT NULL {ts('updated_at', required=True)},

            UNIQUE (account_id, provider_mailbox_id)
        )
        """,
        "DROP TABLE mailboxes",
    ),
    step(
        f"""
        CREATE TABLE threads (
            id                   TEXT PRIMARY KEY,
            account_id           TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            provider_thread_id   TEXT,
            provider_history_id  TEXT,
            -- 'provider' or 'reconstructed'. Consumers doing thread
            -- analysis need to know which they are looking at.
            thread_source        TEXT CHECK (
                thread_source IN ('provider', 'reconstructed')
            ),

            subject           TEXT,
            first_message_at  TEXT {ts('first_message_at')},
            last_message_at   TEXT {ts('last_message_at')},
            message_count     INTEGER NOT NULL DEFAULT 0,
            participants      TEXT,
            snippet           TEXT,
            updated_at        TEXT NOT NULL {ts('updated_at', required=True)},

            UNIQUE (account_id, provider_thread_id)
        )
        """,
        "DROP TABLE threads",
    ),
    step(
        f"""
        CREATE TABLE messages (
            -- Explicit rowid alias so message_fts has a STABLE
            -- content_rowid. Internal; never exposed (ADR 0001).
            seq  INTEGER PRIMARY KEY,
            id   TEXT NOT NULL UNIQUE,

            account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            thread_id   TEXT REFERENCES threads(id) ON DELETE SET NULL,

            provider_message_id  TEXT NOT NULL,
            provider_thread_id   TEXT,

            -- RFC-822 threading headers. Without these, threading a
            -- provider that exposes no thread id is impossible.
            message_id_header   TEXT,
            in_reply_to         TEXT,
            references_header   TEXT,

            -- Canonical folder for single-folder providers; NULL for
            -- Gmail. message_labels is authoritative (ADR 0003).
            mailbox_id  TEXT REFERENCES mailboxes(id) ON DELETE SET NULL,

            subject       TEXT,
            sender_name   TEXT,
            sender_email  TEXT,

            recipients  TEXT,
            cc          TEXT,
            bcc         TEXT,
            reply_to    TEXT,

            sent_at        TEXT {ts('sent_at')},
            received_at    TEXT {ts('received_at')},
            -- Gmail's authoritative ordering key. The Date: header is
            -- client-supplied and routinely wrong or forged.
            internal_date  TEXT {ts('internal_date')},

            body_text  TEXT,
            body_html  TEXT,   -- NULL unless sync.retain_html
            snippet    TEXT,

            has_attachments  INTEGER NOT NULL DEFAULT 0
                             CHECK (has_attachments IN (0, 1)),

            history_id     TEXT,
            size_estimate  INTEGER,

            is_read       INTEGER CHECK (is_read      IN (0, 1)),
            is_starred    INTEGER CHECK (is_starred   IN (0, 1)),
            is_draft      INTEGER CHECK (is_draft     IN (0, 1)),
            is_sent       INTEGER CHECK (is_sent      IN (0, 1)),
            is_important  INTEGER CHECK (is_important IN (0, 1)),
            is_spam       INTEGER CHECK (is_spam      IN (0, 1)),
            is_trash      INTEGER CHECK (is_trash     IN (0, 1)),
            is_answered   INTEGER CHECK (is_answered  IN (0, 1)),

            -- Bulk-mail signals lifted from headers at sync time. These
            -- turn newsletter triage into a WHERE clause instead of a
            -- judgment call, which is the highest-value query this
            -- database serves.
            list_id           TEXT,
            list_unsubscribe  TEXT,
            precedence        TEXT,
            auto_submitted    TEXT,

            provider_metadata  TEXT,

            created_at  TEXT NOT NULL {ts('created_at', required=True)},
            updated_at  TEXT NOT NULL {ts('updated_at', required=True)},
            deleted_at  TEXT {ts('deleted_at')},

            UNIQUE (account_id, provider_message_id)
        )
        """,
        "DROP TABLE messages",
    ),
    step(
        """
        CREATE INDEX idx_messages_account_received
            ON messages(account_id, received_at DESC)
        """,
        "DROP INDEX idx_messages_account_received",
    ),
    step(
        "CREATE INDEX idx_messages_received ON messages(received_at DESC)",
        "DROP INDEX idx_messages_received",
    ),
    step(
        "CREATE INDEX idx_messages_sender ON messages(sender_email)",
        "DROP INDEX idx_messages_sender",
    ),
    step(
        "CREATE INDEX idx_messages_thread ON messages(thread_id)",
        "DROP INDEX idx_messages_thread",
    ),
    step(
        "CREATE INDEX idx_messages_msgid_header ON messages(message_id_header)",
        "DROP INDEX idx_messages_msgid_header",
    ),
    step(
        """
        -- Partial index: almost every query filters deleted rows out, and
        -- deleted_at is NULL for the overwhelming majority of rows.
        CREATE INDEX idx_messages_live
            ON messages(account_id, received_at DESC)
            WHERE deleted_at IS NULL
        """,
        "DROP INDEX idx_messages_live",
    ),
    step(
        """
        CREATE TABLE message_labels (
            message_id  TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            mailbox_id  TEXT NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,

            PRIMARY KEY (message_id, mailbox_id)
        )
        """,
        "DROP TABLE message_labels",
    ),
    step(
        "CREATE INDEX idx_message_labels_mailbox ON message_labels(mailbox_id)",
        "DROP INDEX idx_message_labels_mailbox",
    ),
    step(
        """
        -- Headers repeat legitimately (Received, References), so the key
        -- is (message_id, ordinal) rather than (message_id, name).
        CREATE TABLE message_headers (
            message_id  TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            ordinal     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            name_lower  TEXT NOT NULL,
            value       TEXT,

            PRIMARY KEY (message_id, ordinal)
        )
        """,
        "DROP TABLE message_headers",
    ),
    step(
        """
        CREATE INDEX idx_message_headers_lookup
            ON message_headers(name_lower, message_id)
        """,
        "DROP INDEX idx_message_headers_lookup",
    ),
    step(
        f"""
        CREATE TABLE attachments (
            id          TEXT PRIMARY KEY,
            message_id  TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,

            provider_attachment_id  TEXT,
            part_id                 TEXT,
            content_id              TEXT,
            is_inline               INTEGER NOT NULL DEFAULT 0
                                    CHECK (is_inline IN (0, 1)),

            filename    TEXT,
            mime_type   TEXT,
            size_bytes  INTEGER,

            content_hash  TEXT,
            local_path    TEXT,
            -- Attachments are metadata-only until explicitly retrieved.
            downloaded    INTEGER NOT NULL DEFAULT 0
                          CHECK (downloaded IN (0, 1)),

            provider_metadata  TEXT,
            created_at  TEXT NOT NULL {ts('created_at', required=True)}
        )
        """,
        "DROP TABLE attachments",
    ),
    step(
        "CREATE INDEX idx_attachments_message ON attachments(message_id)",
        "DROP INDEX idx_attachments_message",
    ),
    step(
        f"""
        -- Gmail drafts carry an identifier distinct from the message they
        -- contain; without it, updating a draft accumulates duplicates.
        CREATE TABLE drafts (
            id          TEXT PRIMARY KEY,
            account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,

            provider_draft_id  TEXT,

            message_id              TEXT REFERENCES messages(id) ON DELETE SET NULL,
            in_reply_to_message_id  TEXT REFERENCES messages(id) ON DELETE SET NULL,
            thread_id               TEXT REFERENCES threads(id) ON DELETE SET NULL,

            status  TEXT NOT NULL CHECK (
                status IN ('local', 'synced', 'sent', 'discarded')
            ),

            created_at  TEXT NOT NULL {ts('created_at', required=True)},
            updated_at  TEXT NOT NULL {ts('updated_at', required=True)},
            sent_at     TEXT {ts('sent_at')},

            UNIQUE (account_id, provider_draft_id)
        )
        """,
        "DROP TABLE drafts",
    ),
    step(
        f"""
        CREATE TABLE sync_state (
            account_id  TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,

            provider_cursor  TEXT,
            -- Apple Mail has no change feed, so it carries a per-mailbox
            -- high-water-mark here as JSON instead (ADR 0005).
            mailbox_watermarks  TEXT,

            last_sync_started    TEXT {ts('last_sync_started')},
            last_sync_completed  TEXT {ts('last_sync_completed')},
            last_reconciled_at   TEXT {ts('last_reconciled_at')},

            messages_seen     INTEGER NOT NULL DEFAULT 0,
            messages_added    INTEGER NOT NULL DEFAULT 0,
            messages_updated  INTEGER NOT NULL DEFAULT 0,
            messages_deleted  INTEGER NOT NULL DEFAULT 0,

            status  TEXT CHECK (status IN ('idle', 'running', 'ok', 'failed')),
            error   TEXT
        )
        """,
        "DROP TABLE sync_state",
    ),
    step(
        f"""
        -- Excluded from the query allowlist: a read-only agent should not
        -- be able to inspect the record of write operations (ADR 0002).
        CREATE TABLE audit_log (
            id         TEXT PRIMARY KEY,
            timestamp  TEXT NOT NULL {ts('timestamp', required=True)},

            account_id  TEXT,
            actor       TEXT,
            operation   TEXT NOT NULL,

            resource_type  TEXT,
            resource_id    TEXT,

            request  TEXT,
            result   TEXT
        )
        """,
        "DROP TABLE audit_log",
    ),
    step(
        "CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp DESC)",
        "DROP INDEX idx_audit_log_timestamp",
    ),
    step(
        """
        CREATE VIRTUAL TABLE message_fts USING fts5(
            subject,
            sender_email,
            recipients,
            body_text,
            content='messages',
            content_rowid='seq',
            tokenize="unicode61 remove_diacritics 2"
        )
        """,
        "DROP TABLE message_fts",
    ),
    # Triggers, not application code: a write path cannot forget to call
    # them, and they are transactional with the write that fired them.
    step(
        """
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO message_fts(rowid, subject, sender_email, recipients, body_text)
            VALUES (new.seq, new.subject, new.sender_email, new.recipients, new.body_text);
        END
        """,
        "DROP TRIGGER messages_ai",
    ),
    step(
        """
        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO message_fts(message_fts, rowid, subject, sender_email, recipients, body_text)
            VALUES ('delete', old.seq, old.subject, old.sender_email, old.recipients, old.body_text);
        END
        """,
        "DROP TRIGGER messages_ad",
    ),
    step(
        """
        CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO message_fts(message_fts, rowid, subject, sender_email, recipients, body_text)
            VALUES ('delete', old.seq, old.subject, old.sender_email, old.recipients, old.body_text);
            INSERT INTO message_fts(rowid, subject, sender_email, recipients, body_text)
            VALUES (new.seq, new.subject, new.sender_email, new.recipients, new.body_text);
        END
        """,
        "DROP TRIGGER messages_au",
    ),
]
