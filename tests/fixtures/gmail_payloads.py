"""Synthetic Gmail API payloads.

Shaped after real ``users.messages.get?format=full`` responses: nested
multipart trees, RFC-2047 encoded subjects, URL-safe base64 bodies,
labels standing in for flags, and the bulk-mail headers that make
newsletter triage a WHERE clause.
"""

from __future__ import annotations

import base64
from typing import Any


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def header(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


#: A plain human message: multipart/alternative, both parts present.
SIMPLE_MESSAGE: dict[str, Any] = {
    "id": "18f0a1b2c3d4e5f6",
    "threadId": "18f0a1b2c3d4e500",
    "labelIds": ["INBOX", "UNREAD", "IMPORTANT"],
    "snippet": "The NIH R01 resubmission deadline is October 5.",
    "historyId": "912345",
    "internalDate": "1755680400000",  # 2025-08-20T09:00:00Z
    "sizeEstimate": 4821,
    "payload": {
        "partId": "",
        "mimeType": "multipart/alternative",
        "filename": "",
        "headers": [
            header("From", "Casey Greene <Casey.Greene@example.org>"),
            header("To", "Sean Davis <sean@example.com>, other@example.com"),
            header("Cc", "third@example.com"),
            header("Subject", "=?utf-8?B?UjAxIHJlc3VibWlzc2lvbiB0aW1lbGluZQ==?="),
            header("Date", "Wed, 20 Aug 2025 03:00:00 -0600"),
            header("Message-ID", "<abc123@example.org>"),
            header("In-Reply-To", "<parent@example.org>"),
            header("References", "<root@example.org> <parent@example.org>"),
        ],
        "body": {"size": 0},
        "parts": [
            {
                "partId": "0",
                "mimeType": "text/plain",
                "filename": "",
                "headers": [header("Content-Type", "text/plain; charset=UTF-8")],
                "body": {
                    "size": 92,
                    "data": b64(
                        "The NIH R01 resubmission deadline is October 5.\n"
                        "Can you draft the specific aims by Friday?"
                    ),
                },
            },
            {
                "partId": "1",
                "mimeType": "text/html",
                "filename": "",
                "headers": [header("Content-Type", "text/html; charset=UTF-8")],
                "body": {
                    "size": 140,
                    "data": b64(
                        "<p>The NIH R01 resubmission deadline is October 5.</p>"
                    ),
                },
            },
        ],
    },
}

#: HTML-only bulk mail with every newsletter signal set.
NEWSLETTER_MESSAGE: dict[str, Any] = {
    "id": "18f0a1b2c3d4e5ff",
    "threadId": "18f0a1b2c3d4e5ff",
    "labelIds": ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"],
    "snippet": "Twelve new preprints on single-cell methods.",
    "historyId": "912350",
    "internalDate": "1755939600000",
    "sizeEstimate": 88213,
    "payload": {
        "partId": "",
        "mimeType": "text/html",
        "filename": "",
        "headers": [
            header("From", "Bioinformatics Weekly <digest@newsletter.example>"),
            header("To", "sean@example.com"),
            header("Subject", "This week in computational biology"),
            header("List-Id", "Weekly digest <biweekly.newsletter.example>"),
            header("List-Unsubscribe", "<https://newsletter.example/u/123>"),
            header("Precedence", "bulk"),
            header("Auto-Submitted", "auto-generated"),
        ],
        "body": {
            "size": 200,
            "data": b64(
                "<html><body><h1>This week</h1>"
                "<p>Twelve new preprints on single-cell methods.</p>"
                "</body></html>"
            ),
        },
    },
}

#: Attachments: one real, one inline image referenced by Content-ID.
MESSAGE_WITH_ATTACHMENTS: dict[str, Any] = {
    "id": "18f0a1b2c3d4e600",
    "threadId": "18f0a1b2c3d4e600",
    "labelIds": ["INBOX"],
    "snippet": "Agenda attached.",
    "historyId": "912360",
    "internalDate": "1755849600000",
    "sizeEstimate": 52000,
    "payload": {
        "partId": "",
        "mimeType": "multipart/mixed",
        "filename": "",
        "headers": [
            header("From", "Wells Messersmith <wells@cuanschutz.edu>"),
            header("To", "sean@example.com"),
            header("Subject", "DBMI faculty meeting agenda"),
        ],
        "body": {"size": 0},
        "parts": [
            {
                "partId": "0",
                "mimeType": "text/plain",
                "filename": "",
                "headers": [],
                "body": {"size": 20, "data": b64("Agenda attached.")},
            },
            {
                "partId": "1",
                "mimeType": "application/pdf",
                "filename": "=?utf-8?q?agenda=5Ffinal.pdf?=",
                "headers": [
                    header("Content-Disposition", 'attachment; filename="agenda.pdf"')
                ],
                "body": {"size": 48213, "attachmentId": "ANGjdJ_attach_1"},
            },
            {
                "partId": "2",
                "mimeType": "image/png",
                "filename": "logo.png",
                "headers": [
                    header("Content-ID", "<logo@cuanschutz.edu>"),
                    header("Content-Disposition", "inline; filename=logo.png"),
                ],
                "body": {"size": 3200, "attachmentId": "ANGjdJ_attach_2"},
            },
        ],
    },
}

LABELS_RESPONSE: dict[str, Any] = {
    "labels": [
        {
            "id": "INBOX",
            "name": "INBOX",
            "type": "system",
            "messagesTotal": 1240,
            "messagesUnread": 12,
            "threadsTotal": 900,
            "threadsUnread": 10,
        },
        {
            "id": "Label_12",
            "name": "Grants",
            "type": "user",
            "messageListVisibility": "show",
            "labelListVisibility": "labelShow",
            "color": {"backgroundColor": "#fb4c2f", "textColor": "#ffffff"},
            "messagesTotal": 84,
            "messagesUnread": 0,
        },
    ]
}

#: A history page where one message is added and later deleted, and
#: another gains a label. Deletion must win.
HISTORY_RECORDS: list[dict[str, Any]] = [
    {
        "id": "912346",
        "messagesAdded": [{"message": {"id": "msg-a", "threadId": "t-a"}}],
    },
    {
        "id": "912347",
        "labelsAdded": [{"message": {"id": "msg-b", "threadId": "t-b"}}],
    },
    {
        "id": "912348",
        "messagesDeleted": [{"message": {"id": "msg-a", "threadId": "t-a"}}],
    },
    {
        "id": "912349",
        "labelsRemoved": [{"message": {"id": "msg-b", "threadId": "t-b"}}],
    },
]
