"""Gmail normalization.

Pure functions: no network, no clock, no credentials. This is where the
subtle work is, so this is where the tests are.
"""

from __future__ import annotations

import pytest

from maildb.ids import is_timestamp
from maildb.providers.gmail import (
    collect_history,
    decode_body,
    decode_mime_words,
    normalize_label,
    normalize_message,
    parse_addresses,
    parse_date_header,
    parse_internal_date,
)
from tests.fixtures.gmail_payloads import (
    HISTORY_RECORDS,
    LABELS_RESPONSE,
    MESSAGE_WITH_ATTACHMENTS,
    NEWSLETTER_MESSAGE,
    SIMPLE_MESSAGE,
)


def test_decodes_rfc2047_subject() -> None:
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.subject == "R01 resubmission timeline"


def test_malformed_encoded_word_does_not_break_sync() -> None:
    """A slightly wrong subject beats a failed sync."""
    assert decode_mime_words("=?bogus-charset?B?zzzz?=") is not None


def test_addresses_are_parsed_and_lowercased() -> None:
    """Mixed-case addresses would make sender_email lookups unreliable."""
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.sender is not None
    assert message.sender.email == "casey.greene@example.org"
    assert message.sender.name == "Casey Greene"
    assert [r.email for r in message.recipients] == [
        "sean@example.com",
        "other@example.com",
    ]
    assert [c.email for c in message.cc] == ["third@example.com"]


def test_threading_headers_are_kept() -> None:
    """Without these, threading a provider with no thread id is impossible."""
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.message_id_header == "<abc123@example.org>"
    assert message.in_reply_to == "<parent@example.org>"
    assert "<root@example.org>" in (message.references_header or "")


def test_internal_date_is_authoritative() -> None:
    """The Date header is client-supplied; internalDate is not."""
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.internal_date == "2025-08-20T09:00:00Z"
    assert message.received_at == message.internal_date
    # The Date header says 03:00 -0600, i.e. the same instant, parsed
    # independently and normalized to UTC.
    assert message.sent_at == "2025-08-20T09:00:00Z"


def test_labels_become_flags_with_unread_inverted() -> None:
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.is_read is False  # UNREAD present
    assert message.is_important is True
    assert message.is_starred is False
    assert message.is_spam is False


def test_read_state_is_absence_of_unread() -> None:
    payload = dict(SIMPLE_MESSAGE)
    payload["labelIds"] = ["INBOX"]
    assert normalize_message(payload).is_read is True


def test_bodies_are_decoded() -> None:
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.body_text is not None
    assert "specific aims" in message.body_text


def test_html_is_discarded_unless_retained() -> None:
    """ADR 0008: HTML dominates database size and is never indexed."""
    assert normalize_message(SIMPLE_MESSAGE).body_html is None
    retained = normalize_message(SIMPLE_MESSAGE, retain_html=True)
    assert retained.body_html is not None


def test_html_only_message_gets_synthesized_text() -> None:
    """Newsletters are usually HTML-only, and are what we most want indexed."""
    message = normalize_message(NEWSLETTER_MESSAGE)
    assert message.body_text is not None
    assert "preprints" in message.body_text
    assert "<html>" not in message.body_text


def test_bulk_signals_are_lifted_into_columns() -> None:
    """The highest-value query this database serves, as a WHERE clause."""
    message = normalize_message(NEWSLETTER_MESSAGE)
    assert message.list_id == "Weekly digest <biweekly.newsletter.example>"
    assert message.list_unsubscribe == "<https://newsletter.example/u/123>"
    assert message.precedence == "bulk"
    assert message.auto_submitted == "auto-generated"


def test_human_mail_has_no_bulk_signals() -> None:
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.list_id is None
    assert message.precedence is None


def test_attachments_including_inline_parts() -> None:
    message = normalize_message(MESSAGE_WITH_ATTACHMENTS)
    by_name = {a.filename: a for a in message.attachments}
    assert set(by_name) == {"agenda_final.pdf", "logo.png"}

    pdf = by_name["agenda_final.pdf"]
    assert pdf.provider_attachment_id == "ANGjdJ_attach_1"
    assert pdf.mime_type == "application/pdf"
    assert pdf.size_bytes == 48213
    assert pdf.is_inline is False

    logo = by_name["logo.png"]
    assert logo.is_inline is True
    assert logo.content_id == "logo@cuanschutz.edu"


def test_body_parts_are_not_mistaken_for_attachments() -> None:
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.attachments == []


def test_all_headers_are_preserved_in_order() -> None:
    message = normalize_message(SIMPLE_MESSAGE)
    assert [n for n, _ in message.headers][:3] == ["From", "To", "Cc"]


def test_labels_are_recorded_for_the_junction_table() -> None:
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.mailbox_ids == ["IMPORTANT", "INBOX", "UNREAD"]


def test_size_and_history_are_captured() -> None:
    message = normalize_message(SIMPLE_MESSAGE)
    assert message.size_estimate == 4821
    assert message.history_id == "912345"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("not-a-number", None),
        ("1755680400000", "2025-08-20T09:00:00Z"),
    ],
)
def test_internal_date_parsing_is_total(
    value: str | None, expected: str | None
) -> None:
    assert parse_internal_date(value) == expected


@pytest.mark.parametrize(
    "value", [None, "", "garbage", "Wed, 32 Foo 2025 99:99:99 +9999"]
)
def test_date_header_parsing_never_raises(value: str | None) -> None:
    """Malformed Date headers are common; they must not abort a sync."""
    assert parse_date_header(value) is None or is_timestamp(
        parse_date_header(value) or ""
    )


def test_bad_base64_is_survivable() -> None:
    assert decode_body("!!!not base64!!!") is None


def test_empty_address_header() -> None:
    assert parse_addresses(None) == []
    assert parse_addresses("") == []


def test_label_normalization() -> None:
    labels = [normalize_label(x) for x in LABELS_RESPONSE["labels"]]
    inbox, grants = labels
    assert inbox.mailbox_type == "inbox"
    assert inbox.label_type == "system"
    assert inbox.messages_total == 1240

    assert grants.mailbox_type == "user"
    assert grants.color_background == "#fb4c2f"


def test_history_deletion_wins_over_addition() -> None:
    """The same message can be added and deleted within one page."""
    changed, deleted = collect_history(HISTORY_RECORDS)
    assert deleted == {"msg-a"}
    assert changed == {"msg-b"}
    assert not (changed & deleted)


def test_empty_history_is_not_an_error() -> None:
    assert collect_history([]) == (set(), set())
