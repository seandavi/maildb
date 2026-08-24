"""Seed the provider capability matrix.

This table is what stops the superset schema from lying by omission. Every
column that is not universally populated names its governing capability in
the schema metadata, and ``describe_table`` surfaces the caveat.
"""

from yoyo import step

__depends__ = {"0001_initial"}

# (capability, gmail, apple_mail, gmail note, apple_mail note)
CAPABILITIES = [
    ("incremental_history", 1, 0,
     "users.history.list from a stored historyId",
     "no change feed exists; high-water-mark scan on date received only"),
    ("deletion_events", 1, 0,
     "reported explicitly by history.list",
     "structurally invisible; detected only by the reconciliation sweep"),
    ("flag_change_events", 1, 0,
     "reported explicitly by history.list",
     "structurally invisible; detected only by the reconciliation sweep"),
    ("labels_many_to_many", 1, 0,
     "a message carries many labels",
     "exactly one folder per message; mailbox_id mirrors message_labels"),
    ("native_thread_id", 1, 0,
     "threadId supplied by the API",
     "reconstructed from References/In-Reply-To; see threads.thread_source"),
    ("importance_flag", 1, 0,
     "IMPORTANT label",
     "not exposed; is_important is always NULL"),
    ("spam_trash_state", 1, 1,
     "SPAM and TRASH labels",
     "partial: junk mailbox membership only"),
    ("size_estimate", 1, 0,
     "sizeEstimate on every message",
     "not exposed; size_estimate is always NULL"),
    ("raw_headers", 1, 1,
     "full header list with format=full",
     "all headers of message"),
    ("attachment_by_id", 1, 0,
     "stable attachmentId per part",
     "partial: addressed by name and index, which is not stable across edits"),
    ("server_side_search", 1, 0,
     "the q= query parameter",
     "no usable server-side search; all filtering is local"),
    ("drafts", 1, 1,
     "first-class draft resources with their own ids",
     "limited: drafts can be created but carry no stable provider id"),
    ("send", 1, 1, "users.messages.send", "via AppleScript"),
    ("modify_labels", 1, 1,
     "arbitrary label add/remove",
     "move between mailboxes only"),
]

_rows = ",\n            ".join(
    "('{p}', '{c}', {s}, '{n}')".format(
        p=provider, c=capability, s=supported, n=note.replace("'", "''")
    )
    for capability, gmail, apple, gmail_note, apple_note in CAPABILITIES
    for provider, supported, note in (
        ("gmail", gmail, gmail_note),
        ("apple_mail", apple, apple_note),
    )
)

steps = [
    step(
        f"""
        INSERT INTO provider_capabilities (provider, capability, supported, notes)
        VALUES
            {_rows}
        """,
        "DELETE FROM provider_capabilities",
    ),
]
