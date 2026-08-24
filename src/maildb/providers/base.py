"""The provider seam.

Everything above this line is provider-agnostic. Nothing above it may
import ``gmail`` or ``apple_mail`` directly, and nothing above it may be
shaped around what one provider happens to be unable to do.

Providers differ enormously in what they can observe -- Gmail has a
change feed, Apple Mail has nothing of the kind -- so they declare their
limits through :meth:`EmailProvider.capabilities` rather than leaving the
sync engine to infer them from silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from maildb.domain.enums import Capability, Provider


@dataclass(frozen=True)
class NormalizedAddress:
    name: str | None
    email: str


@dataclass(frozen=True)
class NormalizedMailbox:
    """A label or folder, already in canonical terms."""

    provider_mailbox_id: str
    name: str
    mailbox_type: str | None = None
    label_type: str | None = None
    message_list_visibility: str | None = None
    label_list_visibility: str | None = None
    messages_total: int | None = None
    messages_unread: int | None = None
    threads_total: int | None = None
    threads_unread: int | None = None
    color_background: str | None = None
    color_text: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedAttachment:
    provider_attachment_id: str | None
    part_id: str | None
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    content_id: str | None = None
    is_inline: bool = False


@dataclass(frozen=True)
class NormalizedMessage:
    """A message in canonical terms, ready to upsert.

    Fields a provider cannot supply are ``None``. That is not a defect --
    it is the superset schema working as intended, and
    ``provider_capabilities`` records which absences are structural.
    """

    provider_message_id: str
    provider_thread_id: str | None = None

    message_id_header: str | None = None
    in_reply_to: str | None = None
    references_header: str | None = None

    subject: str | None = None
    sender: NormalizedAddress | None = None
    recipients: list[NormalizedAddress] = field(default_factory=list)
    cc: list[NormalizedAddress] = field(default_factory=list)
    bcc: list[NormalizedAddress] = field(default_factory=list)
    reply_to: list[NormalizedAddress] = field(default_factory=list)

    sent_at: str | None = None
    received_at: str | None = None
    internal_date: str | None = None

    body_text: str | None = None
    body_html: str | None = None
    snippet: str | None = None

    is_read: bool | None = None
    is_starred: bool | None = None
    is_draft: bool | None = None
    is_sent: bool | None = None
    is_important: bool | None = None
    is_spam: bool | None = None
    is_trash: bool | None = None
    is_answered: bool | None = None

    list_id: str | None = None
    list_unsubscribe: str | None = None
    precedence: str | None = None
    auto_submitted: str | None = None

    history_id: str | None = None
    size_estimate: int | None = None

    #: Provider mailbox ids this message belongs to. Many for Gmail,
    #: exactly one for folder-based providers.
    mailbox_ids: list[str] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    attachments: list[NormalizedAttachment] = field(default_factory=list)
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncCursor:
    """Where a previous sync left off.

    ``token`` is a provider-native watermark (Gmail's ``historyId``).
    ``watermarks`` is the per-mailbox fallback for providers with no
    change feed, which have nothing better than "the newest thing I saw
    in this folder".
    """

    token: str | None = None
    watermarks: dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.token and not self.watermarks


@dataclass
class SyncResult:
    """What a provider found, and where to resume."""

    messages: list[NormalizedMessage] = field(default_factory=list)
    mailboxes: list[NormalizedMailbox] = field(default_factory=list)
    #: Provider message ids observed to be gone. Only providers with
    #: ``DELETION_EVENTS`` populate this from an incremental pass; others
    #: fill it from a reconciliation sweep.
    deleted_provider_ids: list[str] = field(default_factory=list)
    cursor: SyncCursor = field(default_factory=SyncCursor)
    #: True when the provider could not resume incrementally and returned
    #: a full listing instead. The engine must then treat absence as
    #: deletion rather than as no-change.
    was_full_sync: bool = False


@dataclass(frozen=True)
class ReconcileEntry:
    """One row of a reconciliation sweep: identity and flags, never bodies."""

    provider_message_id: str
    is_read: bool | None = None
    is_starred: bool | None = None


@runtime_checkable
class EmailProvider(Protocol):
    """What every mail source must implement."""

    provider: Provider
    account_id: str

    def capabilities(self) -> set[Capability]:
        """What this provider can actually do.

        Declared rather than inferred, so the engine never mistakes "not
        supported" for "nothing changed".
        """
        ...

    def list_mailboxes(self) -> list[NormalizedMailbox]: ...

    def sync(self, cursor: SyncCursor | None) -> SyncResult: ...

    def reconcile(self, mailbox_id: str) -> list[ReconcileEntry]:
        """Identity and flags for every message in a mailbox.

        The only way to observe deletions and flag changes for a provider
        without a change feed, and a backstop against an expired cursor
        for one that has it.
        """
        ...

    def get_message(self, provider_message_id: str) -> NormalizedMessage: ...

    def get_attachment(
        self, provider_message_id: str, provider_attachment_id: str
    ) -> bytes: ...
