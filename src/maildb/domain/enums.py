"""Closed vocabularies shared across providers, storage, and the API."""

from __future__ import annotations

from enum import StrEnum


class Provider(StrEnum):
    """Supported mail sources.

    ``GMAIL`` is the reference provider; the canonical schema is built to
    its fidelity. ``APPLE_MAIL`` is a stopgap bridge to Microsoft 365 and
    fills only part of that schema (see :class:`Capability`).
    """

    GMAIL = "gmail"
    APPLE_MAIL = "apple_mail"


class AuthMethod(StrEnum):
    """How an account authenticates.

    Determined by the *kind* of account, not by preference:
    ``SERVICE_ACCOUNT`` requires a Google Workspace admin console to
    authorize domain-wide delegation, so it cannot reach a personal
    ``@gmail.com`` address at all.
    """

    OAUTH_INSTALLED = "oauth_installed"
    SERVICE_ACCOUNT = "service_account"
    NONE = "none"


class MailboxType(StrEnum):
    """Normalized mailbox roles, across Gmail labels and IMAP folders."""

    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    SPAM = "spam"
    ARCHIVE = "archive"
    STARRED = "starred"
    IMPORTANT = "important"
    USER = "user"


class ThreadSource(StrEnum):
    """Where a thread's identity came from.

    Consumers doing thread analysis need to know whether they are looking
    at a provider-supplied grouping or one we inferred from RFC-822
    headers, because the latter is imperfect by construction.
    """

    PROVIDER = "provider"
    RECONSTRUCTED = "reconstructed"


class DraftStatus(StrEnum):
    LOCAL = "local"
    SYNCED = "synced"
    SENT = "sent"
    DISCARDED = "discarded"


class SyncStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


class Capability(StrEnum):
    """What a provider can actually do.

    The schema is a superset built to Gmail. Without an explicit record of
    which provider supports what, a query like ``WHERE is_important = 1``
    returns nothing for the Apple Mail account with no error to say why.
    Every non-universal column names its governing capability in the
    schema metadata.
    """

    INCREMENTAL_HISTORY = "incremental_history"
    DELETION_EVENTS = "deletion_events"
    FLAG_CHANGE_EVENTS = "flag_change_events"
    LABELS_MANY_TO_MANY = "labels_many_to_many"
    NATIVE_THREAD_ID = "native_thread_id"
    IMPORTANCE_FLAG = "importance_flag"
    SPAM_TRASH_STATE = "spam_trash_state"
    SIZE_ESTIMATE = "size_estimate"
    RAW_HEADERS = "raw_headers"
    ATTACHMENT_BY_ID = "attachment_by_id"
    SERVER_SIDE_SEARCH = "server_side_search"
    DRAFTS = "drafts"
    SEND = "send"
    MODIFY_LABELS = "modify_labels"


class WriteOperation(StrEnum):
    """Write operations, for audit logging and the confirmation policy."""

    MARK_READ = "mark_read"
    ARCHIVE = "archive"
    MOVE = "move"
    DELETE = "delete"
    CREATE_DRAFT = "create_draft"
    SEND = "send"
    REPLY = "reply"
    FORWARD = "forward"


class ConfirmationClass(StrEnum):
    """How much human assent a write requires (SPEC section 20)."""

    ALLOWED = "allowed"
    CONFIRM = "confirm"
    EXPLICIT = "explicit"
