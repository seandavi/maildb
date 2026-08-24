"""Gmail provider — the reference implementation.

Split deliberately in two:

*Normalization* is pure. Given a decoded API payload it produces a
:class:`~maildb.providers.base.NormalizedMessage` with no network, no
clock, and no credentials. That is where the subtle work lives -- MIME
walking, RFC-2047 decoding, label-to-flag mapping, bulk-mail signals --
so it is where the tests live too.

*Transport* wraps ``google-api-python-client``. Token refresh, batching,
and retry are its problem, not ours (ADR 0011).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from maildb.domain.enums import Capability, Provider
from maildb.domain.errors import ProviderError
from maildb.ids import to_timestamp
from maildb.providers.base import (
    NormalizedAddress,
    NormalizedAttachment,
    NormalizedMailbox,
    NormalizedMessage,
    ReconcileEntry,
    SyncCursor,
    SyncResult,
)

GMAIL_CAPABILITIES: set[Capability] = {
    Capability.INCREMENTAL_HISTORY,
    Capability.DELETION_EVENTS,
    Capability.FLAG_CHANGE_EVENTS,
    Capability.LABELS_MANY_TO_MANY,
    Capability.NATIVE_THREAD_ID,
    Capability.IMPORTANCE_FLAG,
    Capability.SPAM_TRASH_STATE,
    Capability.SIZE_ESTIMATE,
    Capability.RAW_HEADERS,
    Capability.ATTACHMENT_BY_ID,
    Capability.SERVER_SIDE_SEARCH,
    Capability.DRAFTS,
    Capability.SEND,
    Capability.MODIFY_LABELS,
}

#: Gmail expresses message state as label membership rather than flags.
#: Note the inversion on UNREAD: absence means read.
_SYSTEM_LABELS = {
    "UNREAD": "is_read",
    "STARRED": "is_starred",
    "IMPORTANT": "is_important",
    "SPAM": "is_spam",
    "TRASH": "is_trash",
    "DRAFT": "is_draft",
    "SENT": "is_sent",
}

_MAILBOX_TYPES = {
    "INBOX": "inbox",
    "SENT": "sent",
    "DRAFT": "drafts",
    "TRASH": "trash",
    "SPAM": "spam",
    "STARRED": "starred",
    "IMPORTANT": "important",
}

# --------------------------------------------------------------------------
# Normalization (pure)
# --------------------------------------------------------------------------


def decode_mime_words(value: str | None) -> str | None:
    """Decode RFC-2047 encoded-words, e.g. ``=?utf-8?B?...?=``.

    Subjects and filenames arrive encoded whenever they leave ASCII.
    Never regex this; the stdlib knows the charset rules.
    """
    if value is None:
        return None
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        # Malformed encoded-words are common in the wild. A slightly wrong
        # subject beats a failed sync.
        return value


def header_map(headers: list[dict[str, str]]) -> dict[str, str]:
    """Lowercased header lookup, last value wins for duplicates."""
    return {h["name"].lower(): h.get("value", "") for h in headers if h.get("name")}


def header_pairs(headers: list[dict[str, str]]) -> list[tuple[str, str]]:
    """Header list preserving order and duplicates."""
    return [(h["name"], h.get("value", "")) for h in headers if h.get("name")]


def parse_addresses(value: str | None) -> list[NormalizedAddress]:
    """Parse an address header into normalized addresses.

    Addresses are lowercased because case is not significant in the
    domain part and effectively never significant in practice -- and a
    mixed-case index makes ``WHERE sender_email = ...`` unreliable.
    """
    if not value:
        return []
    out = []
    for name, email in getaddresses([value]):
        if not email:
            continue
        out.append(
            NormalizedAddress(
                name=decode_mime_words(name) or None,
                email=email.strip().lower(),
            )
        )
    return out


def parse_internal_date(value: str | None) -> str | None:
    """Convert Gmail's ``internalDate`` (epoch milliseconds) to canonical UTC."""
    if not value:
        return None
    try:
        return to_timestamp(datetime.fromtimestamp(int(value) / 1000, tz=UTC))
    except (ValueError, OverflowError, OSError):
        return None


def parse_date_header(value: str | None) -> str | None:
    """Convert a ``Date:`` header to canonical UTC.

    This header is client-supplied and routinely wrong -- skewed clocks,
    absent zones, outright forgery. Prefer ``internalDate`` for ordering;
    this is kept for completeness.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    try:
        return to_timestamp(parsed)
    except (ValueError, OverflowError):
        return None


def decode_body(data: str | None) -> str | None:
    """Decode Gmail's URL-safe base64 body data."""
    if not data:
        return None
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return None
    return raw.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    """Render HTML to readable plain text.

    Only used when a message carries no plain-text part, which is common
    for newsletters -- exactly the mail whose text we most want indexed.
    """
    import html2text

    converter = html2text.HTML2Text()
    converter.ignore_images = True
    converter.ignore_emphasis = True
    converter.body_width = 0
    return converter.handle(html).strip()


@dataclass
class _Payload:
    """Accumulator for a MIME tree walk."""

    text: str | None = None
    html: str | None = None
    attachments: list[NormalizedAttachment] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.attachments is None:
            self.attachments = []


def walk_payload(part: dict[str, Any], acc: _Payload | None = None) -> _Payload:
    """Walk a Gmail payload tree, collecting bodies and attachments.

    Takes the *first* text/plain and text/html part found, which for
    multipart/alternative is the correct one. Anything with a filename or
    an attachmentId becomes an attachment, inline images included -- they
    are addressable content, and a Content-ID is how a body references
    them.
    """
    acc = acc or _Payload()
    mime_type = part.get("mimeType", "")
    body = part.get("body", {})
    filename = decode_mime_words(part.get("filename") or "") or ""
    headers = header_map(part.get("headers", []))

    is_attachment = bool(filename) or bool(body.get("attachmentId"))

    if is_attachment:
        content_id = headers.get("content-id", "").strip("<>") or None
        disposition = headers.get("content-disposition", "")
        acc.attachments.append(
            NormalizedAttachment(
                provider_attachment_id=body.get("attachmentId"),
                part_id=part.get("partId"),
                filename=filename or None,
                mime_type=mime_type or None,
                size_bytes=body.get("size"),
                content_id=content_id,
                is_inline="inline" in disposition.lower() or bool(content_id),
            )
        )
    elif mime_type == "text/plain" and acc.text is None:
        acc.text = decode_body(body.get("data"))
    elif mime_type == "text/html" and acc.html is None:
        acc.html = decode_body(body.get("data"))

    for child in part.get("parts", []) or []:
        walk_payload(child, acc)

    return acc


def normalize_message(
    payload: dict[str, Any], *, retain_html: bool = False
) -> NormalizedMessage:
    """Turn a Gmail ``users.messages.get`` response into canonical form."""
    message_id = payload.get("id")
    if not message_id:
        raise ProviderError("Gmail message payload has no id")

    body_part = payload.get("payload", {}) or {}
    headers = body_part.get("headers", []) or []
    lookup = header_map(headers)
    walked = walk_payload(body_part)

    body_text = walked.text
    if body_text is None and walked.html:
        # Newsletters are frequently HTML-only, and they are exactly the
        # mail whose text is worth indexing.
        body_text = html_to_text(walked.html)

    label_ids = set(payload.get("labelIds", []) or [])
    flags: dict[str, bool | None] = {field: None for field in _SYSTEM_LABELS.values()}
    for label, field in _SYSTEM_LABELS.items():
        flags[field] = label in label_ids
    # Gmail marks unread by presence of a label; read state is its absence.
    flags["is_read"] = "UNREAD" not in label_ids

    internal_date = parse_internal_date(payload.get("internalDate"))

    return NormalizedMessage(
        provider_message_id=message_id,
        provider_thread_id=payload.get("threadId"),
        message_id_header=lookup.get("message-id"),
        in_reply_to=lookup.get("in-reply-to"),
        references_header=lookup.get("references"),
        subject=decode_mime_words(lookup.get("subject")),
        sender=next(iter(parse_addresses(lookup.get("from"))), None),
        recipients=parse_addresses(lookup.get("to")),
        cc=parse_addresses(lookup.get("cc")),
        bcc=parse_addresses(lookup.get("bcc")),
        reply_to=parse_addresses(lookup.get("reply-to")),
        sent_at=parse_date_header(lookup.get("date")),
        # internalDate is authoritative; the Date header is a fallback.
        received_at=internal_date or parse_date_header(lookup.get("date")),
        internal_date=internal_date,
        body_text=body_text,
        body_html=walked.html if retain_html else None,
        snippet=payload.get("snippet"),
        is_read=flags["is_read"],
        is_starred=flags["is_starred"],
        is_draft=flags["is_draft"],
        is_sent=flags["is_sent"],
        is_important=flags["is_important"],
        is_spam=flags["is_spam"],
        is_trash=flags["is_trash"],
        is_answered=None,  # Gmail exposes no per-message answered flag
        list_id=lookup.get("list-id"),
        list_unsubscribe=lookup.get("list-unsubscribe"),
        precedence=lookup.get("precedence"),
        auto_submitted=lookup.get("auto-submitted"),
        history_id=payload.get("historyId"),
        size_estimate=payload.get("sizeEstimate"),
        mailbox_ids=sorted(label_ids),
        headers=header_pairs(headers),
        attachments=walked.attachments,
        provider_metadata={"labelIds": sorted(label_ids)},
    )


def normalize_label(label: dict[str, Any]) -> NormalizedMailbox:
    """Turn a Gmail ``users.labels.get`` response into canonical form."""
    label_id = label.get("id")
    if not label_id:
        raise ProviderError("Gmail label payload has no id")
    color = label.get("color", {}) or {}
    return NormalizedMailbox(
        provider_mailbox_id=label_id,
        name=label.get("name", label_id),
        mailbox_type=_MAILBOX_TYPES.get(label_id, "user"),
        label_type=(label.get("type") or "").lower() or None,
        message_list_visibility=label.get("messageListVisibility"),
        label_list_visibility=label.get("labelListVisibility"),
        messages_total=label.get("messagesTotal"),
        messages_unread=label.get("messagesUnread"),
        threads_total=label.get("threadsTotal"),
        threads_unread=label.get("threadsUnread"),
        color_background=color.get("backgroundColor"),
        color_text=color.get("textColor"),
        provider_metadata={"type": label.get("type")},
    )


def collect_history(
    history_records: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Reduce a ``users.history.list`` response to changed and deleted ids.

    History is an append-only log of individual events, so the same
    message may appear repeatedly and may be both added and deleted
    within one page. Deletion wins: a message removed after being added
    is gone.
    """
    changed: set[str] = set()
    deleted: set[str] = set()

    for record in history_records:
        for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
            for entry in record.get(key, []) or []:
                message = entry.get("message", {})
                if message.get("id"):
                    changed.add(message["id"])
        for entry in record.get("messagesDeleted", []) or []:
            message = entry.get("message", {})
            if message.get("id"):
                deleted.add(message["id"])

    return changed - deleted, deleted


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

#: Gmail bills per-user quota in "units": 250 per second, and a
#: ``messages.get`` costs 5. A large backfill is quota-bound rather than
#: network-bound, so the limiter is load-bearing from the first request,
#: not an optimization to add after the first rate-limit ban.
QUOTA_UNITS_PER_SECOND = 250
COST_MESSAGES_GET = 5
COST_MESSAGES_LIST = 5
COST_LABELS_LIST = 1
COST_HISTORY_LIST = 2

#: Gmail retains history for roughly a week. Past that, `historyId` is
#: gone and the only correct response is a full resynchronization.
HISTORY_EXPIRED_STATUS = 404

SCOPES_READONLY = ["https://www.googleapis.com/auth/gmail.readonly"]
SCOPES_MODIFY = ["https://www.googleapis.com/auth/gmail.modify"]
SCOPES_SEND = ["https://www.googleapis.com/auth/gmail.send"]
SCOPES_COMPOSE = ["https://www.googleapis.com/auth/gmail.compose"]


class TokenBucket:
    """Token bucket over Gmail's per-second quota.

    Deliberately not distributed and not clever: one process, one
    account, one clock. Two things it does have to get right, both found
    the hard way:

    *A request larger than the bucket.* A batch of 100 ``messages.get``
    costs 500 units against a 250-unit bucket, which can never be
    satisfied in one go because the bucket caps at its own rate. Such a
    request is drained in bucket-sized instalments rather than waited on
    forever.

    *Floating-point starvation.* Refilling for exactly the deficit can
    land a hair short (``0.1 * 100 == 9.999...``), leaving a deficit of
    1e-15 that requests a 1e-17 second sleep and makes no progress. Every
    wait has a floor, so each iteration advances.
    """

    #: Floor on any single wait. Guarantees forward progress regardless of
    #: floating-point rounding.
    MIN_DELAY = 0.001

    def __init__(
        self, rate: int = QUOTA_UNITS_PER_SECOND, *, clock: Any = None
    ) -> None:
        import time

        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.tokens = float(rate)
        self._clock = clock or time.monotonic
        self._sleep = time.sleep
        self._updated = self._clock()

    def consume(self, units: int) -> None:
        """Block until ``units`` of quota have been spent.

        A request larger than the bucket is drained in instalments; it is
        a legitimate ask (one HTTP batch really does cost that much), just
        not one a single bucket-full can cover.
        """
        remaining = units
        while remaining > 0:
            instalment = min(remaining, self.rate)
            self._acquire(instalment)
            remaining -= instalment

    def _acquire(self, units: int) -> None:
        while True:
            now = self._clock()
            self.tokens = min(
                float(self.rate), self.tokens + (now - self._updated) * self.rate
            )
            self._updated = now
            if self.tokens >= units:
                self.tokens -= units
                return
            deficit = units - self.tokens
            self._sleep(max(deficit / self.rate, self.MIN_DELAY))


class GmailAuth:
    """Credential acquisition, behind one seam.

    Two mechanisms, chosen by account type rather than preference: a
    service account with domain-wide delegation cannot reach a personal
    ``@gmail.com`` address, because authorizing DWD requires a Workspace
    admin console that a personal account does not have. The config
    loader rejects that combination before we get here.
    """

    def __init__(
        self,
        account_id: str,
        *,
        scopes: list[str] | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        service_account_file: str | None = None,
        delegated_subject: str | None = None,
        redirect_port: int = 0,
    ) -> None:
        self.account_id = account_id
        self.scopes = scopes or SCOPES_READONLY
        self.client_id = client_id
        self.client_secret = client_secret
        self.service_account_file = service_account_file
        self.delegated_subject = delegated_subject
        # Port 0 asks the OS for an ephemeral port, which a *Desktop app*
        # OAuth client may use freely. A Web application client must
        # register its exact redirect URI, and so must pin a port here.
        self.redirect_port = redirect_port

    def service_account_credentials(self) -> Any:
        from google.oauth2 import service_account

        if not self.service_account_file:
            raise ProviderError("no service account key file configured")
        creds = service_account.Credentials.from_service_account_file(
            self.service_account_file, scopes=self.scopes
        )
        if self.delegated_subject:
            creds = creds.with_subject(self.delegated_subject)
        return creds

    def installed_app_credentials(self, stored_token: str | None) -> Any:
        """Reuse a stored refresh token, or run the consent flow once."""
        import json

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        if stored_token:
            creds = Credentials.from_authorized_user_info(
                json.loads(stored_token), self.scopes
            )
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                return creds

        if not (self.client_id and self.client_secret):
            raise ProviderError(
                "no OAuth client configured; expected the client id and "
                "secret in Secret Manager (see decisions/0012)"
            )
        flow = InstalledAppFlow.from_client_config(self.client_config(), self.scopes)
        return flow.run_local_server(
            port=self.redirect_port,
            prompt="consent",
            # Without offline access Google returns no refresh token, and
            # the grant dies with the process.
            access_type="offline",
        )

    def client_config(self) -> dict[str, Any]:
        """Assemble the client-config structure the flow expects.

        Google normally hands this out as a downloaded JSON file. The two
        halves are stored separately here, so it is rebuilt rather than
        read.
        """
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }

    @staticmethod
    def to_stored_token(credentials: Any) -> str:
        """Serialize credentials for the keychain."""
        return str(credentials.to_json())


class GmailProvider:
    """Gmail, via the official API client."""

    provider = Provider.GMAIL

    def __init__(
        self,
        account_id: str,
        service: Any,
        *,
        retain_html: bool = False,
        limiter: TokenBucket | None = None,
        page_size: int = 500,
        batch_size: int = 100,
    ) -> None:
        self.account_id = account_id
        self.service = service
        self.retain_html = retain_html
        self.limiter = limiter or TokenBucket()
        self.page_size = page_size
        self.batch_size = batch_size

    def capabilities(self) -> set[Capability]:
        return set(GMAIL_CAPABILITIES)

    # -- reads -----------------------------------------------------------

    def list_mailboxes(self) -> list[NormalizedMailbox]:
        self.limiter.consume(COST_LABELS_LIST)
        response = self.service.users().labels().list(userId="me").execute()
        return [normalize_label(label) for label in response.get("labels", [])]

    def get_message(self, provider_message_id: str) -> NormalizedMessage:
        self.limiter.consume(COST_MESSAGES_GET)
        payload = (
            self.service.users()
            .messages()
            .get(userId="me", id=provider_message_id, format="full")
            .execute()
        )
        return normalize_message(payload, retain_html=self.retain_html)

    def get_attachment(
        self, provider_message_id: str, provider_attachment_id: str
    ) -> bytes:
        self.limiter.consume(COST_MESSAGES_GET)
        response = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=provider_message_id, id=provider_attachment_id)
            .execute()
        )
        data = response.get("data")
        if not data:
            raise ProviderError(f"attachment {provider_attachment_id} returned no data")
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    def _list_message_ids(self, query: str | None = None) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        while True:
            self.limiter.consume(COST_MESSAGES_LIST)
            request = {
                "userId": "me",
                "maxResults": self.page_size,
                "includeSpamTrash": True,
            }
            if query:
                request["q"] = query
            if page_token:
                request["pageToken"] = page_token
            response = self.service.users().messages().list(**request).execute()
            ids.extend(m["id"] for m in response.get("messages", []) or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return ids

    def _fetch_messages(self, message_ids: list[str]) -> list[NormalizedMessage]:
        """Fetch full messages in HTTP batches.

        Batching is the difference between a backfill that finishes and
        one that does not: a per-message round trip on a large mailbox is
        dominated by latency, not quota.
        """
        collected: list[NormalizedMessage] = []
        failures: list[tuple[str, Exception]] = []

        for start in range(0, len(message_ids), self.batch_size):
            chunk = message_ids[start : start + self.batch_size]
            self.limiter.consume(COST_MESSAGES_GET * len(chunk))

            def callback(
                request_id: str, response: Any, error: Exception | None
            ) -> None:
                if error is not None:
                    failures.append((request_id, error))
                    return
                collected.append(
                    normalize_message(response, retain_html=self.retain_html)
                )

            batch = self.service.new_batch_http_request(callback=callback)
            for message_id in chunk:
                batch.add(
                    self.service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="full"),
                    request_id=message_id,
                )
            batch.execute()

        if failures:
            # Partial failure is not silent success: the caller decides
            # whether to retry, but it is told.
            raise ProviderError(
                f"{len(failures)} of {len(message_ids)} message fetches failed; "
                f"first: {failures[0][0]} -> {failures[0][1]}"
            )
        return collected

    def reconcile(self, mailbox_id: str) -> list[ReconcileEntry]:
        """Identity and flags for a label, without fetching bodies."""
        entries: list[ReconcileEntry] = []
        page_token: str | None = None
        while True:
            self.limiter.consume(COST_MESSAGES_LIST)
            request = {
                "userId": "me",
                "labelIds": [mailbox_id],
                "maxResults": self.page_size,
                "includeSpamTrash": True,
            }
            if page_token:
                request["pageToken"] = page_token
            response = self.service.users().messages().list(**request).execute()
            for stub in response.get("messages", []) or []:
                entries.append(ReconcileEntry(provider_message_id=stub["id"]))
            page_token = response.get("nextPageToken")
            if not page_token:
                return entries

    # -- sync ------------------------------------------------------------

    def sync(self, cursor: SyncCursor | None = None) -> SyncResult:
        """Incremental where possible, full where not."""
        mailboxes = self.list_mailboxes()

        if cursor is None or cursor.is_empty():
            return self._full_sync(mailboxes)

        try:
            return self._incremental_sync(cursor, mailboxes)
        except _HistoryExpired:
            # Gmail keeps history for about a week. An expired cursor is
            # an expected condition after a laptop is closed over a
            # holiday, not an error -- fall back rather than fail.
            return self._full_sync(mailboxes)

    def _full_sync(self, mailboxes: list[NormalizedMailbox]) -> SyncResult:
        message_ids = self._list_message_ids()
        messages = self._fetch_messages(message_ids)
        return SyncResult(
            messages=messages,
            mailboxes=mailboxes,
            cursor=SyncCursor(token=self._current_history_id(messages)),
            was_full_sync=True,
        )

    def _incremental_sync(
        self, cursor: SyncCursor, mailboxes: list[NormalizedMailbox]
    ) -> SyncResult:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        latest = cursor.token

        while True:
            self.limiter.consume(COST_HISTORY_LIST)
            request = {
                "userId": "me",
                "startHistoryId": cursor.token,
                "maxResults": self.page_size,
            }
            if page_token:
                request["pageToken"] = page_token
            try:
                response = self.service.users().history().list(**request).execute()
            except Exception as exc:
                if _is_history_expired(exc):
                    raise _HistoryExpired from exc
                raise

            records.extend(response.get("history", []) or [])
            latest = response.get("historyId", latest)
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        changed, deleted = collect_history(records)
        messages = self._fetch_messages(sorted(changed)) if changed else []

        return SyncResult(
            messages=messages,
            mailboxes=mailboxes,
            deleted_provider_ids=sorted(deleted),
            cursor=SyncCursor(token=latest),
            was_full_sync=False,
        )

    @staticmethod
    def _current_history_id(messages: list[NormalizedMessage]) -> str | None:
        """Highest historyId seen, as the resume point for the next sync."""
        ids = [int(m.history_id) for m in messages if m.history_id]
        return str(max(ids)) if ids else None


class _HistoryExpired(Exception):
    """Gmail no longer holds history back to our cursor."""


def _is_history_expired(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status == HISTORY_EXPIRED_STATUS
