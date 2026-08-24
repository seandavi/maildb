"""A realistic corpus to test against.

Every later milestone tests against this rather than a live mailbox:
threads spanning both providers, Gmail's many-to-many labels next to
Apple Mail's single folder, bulk mail mixed with human correspondence,
attachments, and a deleted message. It is deterministic -- no randomness,
no clock -- so failures are reproducible.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from maildb.db import connection
from maildb.db.migrations import apply_migrations
from maildb.ids import new_id

NOW = "2026-08-24T12:00:00Z"


@dataclass(frozen=True)
class Corpus:
    """Identifiers the tests need to refer back to."""

    path: Path
    gmail_account: str
    apple_account: str
    message_ids: dict[str, str]
    mailbox_ids: dict[str, str]


def _insert(conn: sqlite3.Connection, table: str, **values: object) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def build(path: Path) -> Corpus:
    """Create and populate a database at ``path``."""
    apply_migrations(path)
    conn = connection.writable(path)
    message_ids: dict[str, str] = {}
    mailbox_ids: dict[str, str] = {}

    try:
        conn.execute("BEGIN")

        _insert(
            conn,
            "accounts",
            id="gmail-personal",
            provider="gmail",
            email="sean@example.com",
            display_name="Personal",
            auth_method="oauth_installed",
            oauth_scopes=json.dumps(["gmail.readonly"]),
            credential_ref="maildb-gmail-personal",
            enabled=1,
            sync_enabled=1,
            created_at=NOW,
            updated_at=NOW,
        )
        _insert(
            conn,
            "accounts",
            id="cu",
            provider="apple_mail",
            email="sean@cuanschutz.edu",
            display_name="CU Anschutz",
            auth_method="none",
            enabled=1,
            sync_enabled=1,
            created_at=NOW,
            updated_at=NOW,
        )

        # Gmail labels: many-to-many, with counts and colors.
        for key, name, mtype, label_type in [
            ("g_inbox", "INBOX", "inbox", "system"),
            ("g_important", "IMPORTANT", "important", "system"),
            ("g_grants", "Grants", "user", "user"),
        ]:
            mailbox_ids[key] = new_id()
            _insert(
                conn,
                "mailboxes",
                id=mailbox_ids[key],
                account_id="gmail-personal",
                provider_mailbox_id=name,
                name=name,
                mailbox_type=mtype,
                label_type=label_type,
                messages_total=3,
                messages_unread=1,
                updated_at=NOW,
            )

        # Apple Mail: one folder per message, no label metadata.
        apple_boxes = [("a_inbox", "INBOX", "inbox"), ("a_arch", "Archive", "archive")]
        for key, name, mtype in apple_boxes:
            mailbox_ids[key] = new_id()
            _insert(
                conn,
                "mailboxes",
                id=mailbox_ids[key],
                account_id="cu",
                provider_mailbox_id=name,
                name=name,
                mailbox_type=mtype,
                updated_at=NOW,
            )

        gmail_thread = new_id()
        _insert(
            conn,
            "threads",
            id=gmail_thread,
            account_id="gmail-personal",
            provider_thread_id="t-100",
            thread_source="provider",
            subject="R01 resubmission timeline",
            first_message_at="2026-08-20T09:00:00Z",
            last_message_at="2026-08-21T14:30:00Z",
            message_count=2,
            participants=json.dumps(["sean@example.com", "casey@example.org"]),
            updated_at=NOW,
        )
        apple_thread = new_id()
        _insert(
            conn,
            "threads",
            id=apple_thread,
            account_id="cu",
            provider_thread_id=None,
            thread_source="reconstructed",
            subject="Faculty meeting agenda",
            first_message_at="2026-08-22T08:00:00Z",
            last_message_at="2026-08-22T08:00:00Z",
            message_count=1,
            updated_at=NOW,
        )

        def message(
            key: str,
            *,
            account: str,
            thread: str | None,
            subject: str,
            sender_name: str,
            sender: str,
            received: str,
            body: str,
            labels: list[str],
            mailbox: str | None = None,
            **extra: object,
        ) -> None:
            message_ids[key] = new_id()
            _insert(
                conn,
                "messages",
                id=message_ids[key],
                account_id=account,
                thread_id=thread,
                provider_message_id=f"pm-{key}",
                message_id_header=f"<{key}@example.com>",
                mailbox_id=mailbox,
                subject=subject,
                sender_name=sender_name,
                sender_email=sender,
                recipients=json.dumps(["sean@example.com"]),
                received_at=received,
                sent_at=received,
                body_text=body,
                snippet=body[:60],
                created_at=NOW,
                updated_at=NOW,
                **extra,
            )
            for label in labels:
                _insert(
                    conn,
                    "message_labels",
                    message_id=message_ids[key],
                    mailbox_id=mailbox_ids[label],
                )

        message(
            "grant",
            account="gmail-personal",
            thread=gmail_thread,
            subject="R01 resubmission timeline",
            sender_name="Casey Greene",
            sender="casey@example.org",
            received="2026-08-20T09:00:00Z",
            body="The NIH R01 resubmission deadline is October 5. "
            "Can you draft the specific aims by Friday?",
            labels=["g_inbox", "g_important"],
            internal_date="2026-08-20T09:00:00Z",
            is_read=0,
            is_starred=1,
            is_important=1,
            is_spam=0,
            is_trash=0,
            size_estimate=4821,
            history_id="9001",
        )
        message(
            "grant_reply",
            account="gmail-personal",
            thread=gmail_thread,
            subject="Re: R01 resubmission timeline",
            sender_name="Sean Davis",
            sender="sean@example.com",
            received="2026-08-21T14:30:00Z",
            body="Drafting the aims now. Will circulate Thursday.",
            labels=["g_inbox"],
            internal_date="2026-08-21T14:30:00Z",
            is_read=1,
            is_starred=0,
            is_important=0,
            is_sent=1,
            size_estimate=1204,
            history_id="9002",
        )
        message(
            "newsletter",
            account="gmail-personal",
            thread=None,
            subject="This week in computational biology",
            sender_name="Bioinformatics Weekly",
            sender="digest@newsletter.example",
            received="2026-08-23T06:00:00Z",
            body="Twelve new preprints on single-cell methods this week.",
            labels=["g_inbox"],
            internal_date="2026-08-23T06:00:00Z",
            is_read=0,
            is_important=0,
            size_estimate=88213,
            list_id="<biweekly.newsletter.example>",
            list_unsubscribe="<https://newsletter.example/u>",
            precedence="bulk",
            auto_submitted="auto-generated",
        )
        message(
            "faculty",
            account="cu",
            thread=apple_thread,
            subject="DBMI faculty meeting agenda",
            sender_name="Wells Messersmith",
            sender="wells@cuanschutz.edu",
            received="2026-08-22T08:00:00Z",
            body="Agenda attached for Monday's faculty meeting. "
            "Please review the retreat planning item.",
            labels=["a_inbox"],
            mailbox=mailbox_ids["a_inbox"],
            is_read=1,
            has_attachments=1,
            # is_important, size_estimate, internal_date stay NULL:
            # apple_mail cannot supply them, and the capability table
            # says so.
        )
        message(
            "removed",
            account="cu",
            thread=None,
            subject="Cancelled: retreat planning",
            sender_name="Wells Messersmith",
            sender="wells@cuanschutz.edu",
            received="2026-08-19T11:00:00Z",
            body="This meeting has been cancelled.",
            labels=["a_arch"],
            mailbox=mailbox_ids["a_arch"],
            is_read=1,
            deleted_at="2026-08-23T10:00:00Z",
        )

        _insert(
            conn,
            "attachments",
            id=new_id(),
            message_id=message_ids["faculty"],
            provider_attachment_id=None,
            part_id="2",
            filename="agenda.pdf",
            mime_type="application/pdf",
            size_bytes=48213,
            downloaded=0,
            created_at=NOW,
        )

        for ordinal, (name, value) in enumerate(
            [
                ("From", "Wells Messersmith <wells@cuanschutz.edu>"),
                ("Subject", "DBMI faculty meeting agenda"),
                ("Received", "from mx1.cuanschutz.edu"),
                ("Received", "from mx2.cuanschutz.edu"),
            ]
        ):
            _insert(
                conn,
                "message_headers",
                message_id=message_ids["faculty"],
                ordinal=ordinal,
                name=name,
                name_lower=name.lower(),
                value=value,
            )

        _insert(
            conn,
            "sync_state",
            account_id="gmail-personal",
            provider_cursor="9002",
            last_sync_completed=NOW,
            status="ok",
            messages_seen=3,
            messages_added=3,
        )
        _insert(
            conn,
            "sync_state",
            account_id="cu",
            mailbox_watermarks=json.dumps({"INBOX": "2026-08-22T08:00:00Z"}),
            last_sync_completed=NOW,
            last_reconciled_at=NOW,
            status="ok",
            messages_seen=2,
            messages_added=2,
        )

        _insert(
            conn,
            "audit_log",
            id=new_id(),
            timestamp=NOW,
            account_id="gmail-personal",
            actor="test",
            operation="mark_read",
            resource_type="message",
            resource_id=message_ids["grant_reply"],
            result="ok",
        )

        conn.execute("COMMIT")
    finally:
        conn.close()

    return Corpus(
        path=path,
        gmail_account="gmail-personal",
        apple_account="cu",
        message_ids=message_ids,
        mailbox_ids=mailbox_ids,
    )
