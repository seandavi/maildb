"""Thread reconstruction for providers with no thread identity.

Gmail hands us a ``threadId``. Apple Mail hands us nothing, so threads
have to be rebuilt from RFC-822 headers: union-find over the edges implied
by ``In-Reply-To`` and ``References``.

Implemented here rather than taken from a library because ``jwzthreading``
is effectively unmaintained and this is fifty lines of core domain logic,
not plumbing (ADR 0011).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

_MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@[^<>@\s]+>")

#: Reply prefixes across the languages that actually show up in a mailbox.
_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|aw|sv|vs|fwd?|rv|antw)\s*(?:\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)


def parse_message_ids(value: str | None) -> list[str]:
    """Extract message-ids from a References or In-Reply-To header.

    These headers are whitespace-separated in theory and arbitrarily
    malformed in practice, so extract by pattern rather than by split.
    """
    if not value:
        return []
    return _MESSAGE_ID_RE.findall(value)


def normalize_subject(subject: str | None) -> str:
    """Strip reply and forward prefixes for subject-based fallback grouping."""
    if not subject:
        return ""
    return _REPLY_PREFIX_RE.sub("", subject).strip().casefold()


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression: threads can be long, and this is called once
        # per edge.
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a


class ThreadCandidate:
    """The minimum needed to place a message in a thread."""

    __slots__ = ("key", "message_id_header", "references", "subject")

    def __init__(
        self,
        key: str,
        message_id_header: str | None,
        references: Iterable[str],
        subject: str | None,
    ) -> None:
        self.key = key
        self.message_id_header = message_id_header
        self.references = list(references)
        self.subject = subject


def group_threads(
    candidates: Sequence[ThreadCandidate],
    *,
    use_subject_fallback: bool = True,
) -> dict[str, list[str]]:
    """Group messages into threads.

    Returns a mapping of thread key to the message keys in it. Header
    edges are authoritative. Subject similarity is a fallback for
    messages with no usable headers at all -- it is genuinely lossy (two
    unrelated "Re: quick question" threads will merge), so it applies
    only where headers are absent.
    """
    union = _UnionFind()
    by_message_id: dict[str, str] = {}

    for candidate in candidates:
        union.add(candidate.key)
        if candidate.message_id_header:
            by_message_id[candidate.message_id_header] = candidate.key

    # Header edges.
    for candidate in candidates:
        for reference in candidate.references:
            target = by_message_id.get(reference)
            if target is not None:
                union.union(candidate.key, target)

    # Subject fallback, only for messages no header edge could place.
    if use_subject_fallback:
        orphans = [
            c
            for c in candidates
            if not c.references and not _is_referenced(c, candidates)
        ]
        by_subject: dict[str, str] = {}
        for candidate in orphans:
            subject = normalize_subject(candidate.subject)
            if not subject:
                continue
            if subject in by_subject:
                union.union(by_subject[subject], candidate.key)
            else:
                by_subject[subject] = candidate.key

    groups: dict[str, list[str]] = {}
    for candidate in candidates:
        groups.setdefault(union.find(candidate.key), []).append(candidate.key)
    return groups


def _is_referenced(
    candidate: ThreadCandidate, candidates: Sequence[ThreadCandidate]
) -> bool:
    if not candidate.message_id_header:
        return False
    return any(
        candidate.message_id_header in other.references
        for other in candidates
        if other.key != candidate.key
    )
