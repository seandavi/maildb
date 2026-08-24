"""A provider that returns whatever the test tells it to.

The engine is provider-agnostic, so it can be tested without Gmail,
AppleScript, or a network. This fake also models the *asymmetry* between
providers: it can be constructed with or without a change feed, so the
same engine tests cover both shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from maildb.domain.enums import Capability, Provider
from maildb.providers.base import (
    NormalizedMailbox,
    NormalizedMessage,
    ReconcileEntry,
    SyncCursor,
    SyncResult,
)

GMAIL_LIKE = {
    Capability.INCREMENTAL_HISTORY,
    Capability.DELETION_EVENTS,
    Capability.LABELS_MANY_TO_MANY,
    Capability.NATIVE_THREAD_ID,
}
APPLE_LIKE: set[Capability] = {Capability.RAW_HEADERS}


@dataclass
class FakeProvider:
    """Returns a scripted sequence of sync results."""

    account_id: str = "test"
    provider: Provider = Provider.GMAIL
    caps: set[Capability] = field(default_factory=lambda: set(GMAIL_LIKE))
    results: list[SyncResult] = field(default_factory=list)
    reconcile_entries: dict[str, list[ReconcileEntry]] = field(default_factory=dict)
    #: Cursors the engine handed us, so tests can assert on resumption.
    seen_cursors: list[SyncCursor | None] = field(default_factory=list)
    calls: int = 0
    fail_on_call: int | None = None

    def capabilities(self) -> set[Capability]:
        return set(self.caps)

    def list_mailboxes(self) -> list[NormalizedMailbox]:
        if not self.results:
            return []
        return self.results[min(self.calls, len(self.results) - 1)].mailboxes

    def sync(self, cursor: SyncCursor | None) -> SyncResult:
        self.seen_cursors.append(cursor)
        index = self.calls
        self.calls += 1
        if self.fail_on_call is not None and index == self.fail_on_call:
            raise RuntimeError("provider exploded mid-sync")
        if index >= len(self.results):
            return SyncResult(cursor=cursor or SyncCursor())
        return self.results[index]

    def reconcile(self, mailbox_id: str) -> list[ReconcileEntry]:
        return self.reconcile_entries.get(mailbox_id, [])

    def get_message(self, provider_message_id: str) -> NormalizedMessage:
        for result in self.results:
            for message in result.messages:
                if message.provider_message_id == provider_message_id:
                    return message
        raise KeyError(provider_message_id)

    def get_attachment(
        self, provider_message_id: str, provider_attachment_id: str
    ) -> bytes:
        return b"fake-bytes"
