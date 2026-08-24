"""Thread reconstruction."""

from __future__ import annotations

import pytest

from maildb.sync.threads import (
    ThreadCandidate,
    group_threads,
    normalize_subject,
    parse_message_ids,
)


def _groups(candidates: list[ThreadCandidate], **kwargs: bool) -> list[list[str]]:
    grouped = group_threads(candidates, **kwargs)  # type: ignore[arg-type]
    return sorted((sorted(v) for v in grouped.values()), key=len, reverse=True)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Re: Grant", "grant"),
        ("RE: RE: Grant", "grant"),
        ("Fwd: Re: Grant", "grant"),
        ("Re[2]: Grant", "grant"),
        ("AW: Grant", "grant"),  # German
        ("SV: Grant", "grant"),  # Swedish/Danish
        ("Grant", "grant"),
        (None, ""),
        ("", ""),
    ],
)
def test_subject_normalization(subject: str | None, expected: str) -> None:
    assert normalize_subject(subject) == expected


def test_reply_prefix_inside_the_subject_is_not_stripped() -> None:
    """Only leading prefixes are prefixes."""
    assert normalize_subject("Notes on re: framing") == "notes on re: framing"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, []),
        ("", []),
        ("<a@x.com>", ["<a@x.com>"]),
        ("<a@x.com> <b@x.com>", ["<a@x.com>", "<b@x.com>"]),
        ("<a@x.com>,<b@x.com>", ["<a@x.com>", "<b@x.com>"]),  # malformed but common
        ("  <a@x.com>\r\n\t<b@x.com>  ", ["<a@x.com>", "<b@x.com>"]),
        ("garbage with no ids", []),
    ],
)
def test_message_id_extraction_survives_malformed_headers(
    header: str | None, expected: list[str]
) -> None:
    assert parse_message_ids(header) == expected


def test_reply_chain_forms_one_thread() -> None:
    candidates = [
        ThreadCandidate("a", "<1@x>", [], "Grant"),
        ThreadCandidate("b", "<2@x>", ["<1@x>"], "Re: Grant"),
        ThreadCandidate("c", "<3@x>", ["<1@x>", "<2@x>"], "Re: Grant"),
    ]
    assert _groups(candidates) == [["a", "b", "c"]]


def test_unrelated_messages_stay_apart() -> None:
    candidates = [
        ThreadCandidate("a", "<1@x>", [], "Grant"),
        ThreadCandidate("b", "<2@x>", [], "Budget"),
    ]
    assert _groups(candidates) == [["a"], ["b"]]


def test_out_of_order_arrival() -> None:
    """A reply can be indexed before the message it answers."""
    candidates = [
        ThreadCandidate("reply", "<2@x>", ["<1@x>"], "Re: Grant"),
        ThreadCandidate("root", "<1@x>", [], "Grant"),
    ]
    assert _groups(candidates) == [["reply", "root"]]


def test_reference_to_an_absent_message_does_not_merge_everything() -> None:
    """Half a thread is normal; it must not become a magnet for the rest."""
    candidates = [
        ThreadCandidate("a", "<1@x>", ["<missing@x>"], "Grant"),
        ThreadCandidate("b", "<2@x>", ["<alsomissing@x>"], "Budget"),
    ]
    assert _groups(candidates) == [["a"], ["b"]]


def test_subject_fallback_groups_headerless_messages() -> None:
    candidates = [
        ThreadCandidate("a", None, [], "Retreat planning"),
        ThreadCandidate("b", None, [], "Re: Retreat planning"),
        ThreadCandidate("c", None, [], "Something else"),
    ]
    assert _groups(candidates) == [["a", "b"], ["c"]]


def test_subject_fallback_can_be_disabled() -> None:
    """It is lossy, so it is a choice rather than a default of the algorithm."""
    candidates = [
        ThreadCandidate("a", None, [], "Quick question"),
        ThreadCandidate("b", None, [], "Quick question"),
    ]
    assert _groups(candidates, use_subject_fallback=False) == [["a"], ["b"]]


def test_headers_beat_subject_similarity() -> None:
    """Two threads that share a subject but not a header chain stay apart."""
    candidates = [
        ThreadCandidate("a1", "<1@x>", [], "Quick question"),
        ThreadCandidate("a2", "<2@x>", ["<1@x>"], "Re: Quick question"),
        ThreadCandidate("b1", "<8@x>", [], "Quick question"),
        ThreadCandidate("b2", "<9@x>", ["<8@x>"], "Re: Quick question"),
    ]
    groups = _groups(candidates)
    assert sorted(groups) == [["a1", "a2"], ["b1", "b2"]]


def test_large_chain_terminates() -> None:
    """Path compression, exercised: long threads are real."""
    candidates = [ThreadCandidate("m0", "<0@x>", [], "Long")]
    candidates += [
        ThreadCandidate(f"m{i}", f"<{i}@x>", [f"<{i - 1}@x>"], "Re: Long")
        for i in range(1, 500)
    ]
    assert len(_groups(candidates)[0]) == 500


def test_empty_input() -> None:
    assert group_threads([]) == {}
