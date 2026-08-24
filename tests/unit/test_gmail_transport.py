"""Gmail transport: paging, batching, history, and the expiry fallback."""

from __future__ import annotations

from typing import Any

import pytest

from maildb.domain.enums import Capability
from maildb.domain.errors import ProviderError
from maildb.providers.gmail import GmailProvider, TokenBucket
from tests.fixtures.gmail_payloads import (
    LABELS_RESPONSE,
    NEWSLETTER_MESSAGE,
    SIMPLE_MESSAGE,
)


class _FakeHttpError(Exception):
    """Stands in for googleapiclient.errors.HttpError."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = type("resp", (), {"status": status})()


class _Request:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Batch:
    def __init__(self, service: FakeService, callback: Any) -> None:
        self.service = service
        self.callback = callback
        self.requests: list[tuple[str, _Request]] = []

    def add(self, request: _Request, request_id: str) -> None:
        self.requests.append((request_id, request))

    def execute(self) -> None:
        self.service.batches += 1
        for request_id, request in self.requests:
            error = self.service.batch_errors.get(request_id)
            if error is not None:
                self.callback(request_id, None, error)
            else:
                self.callback(request_id, request._result, None)


class FakeService:
    """The slice of the Gmail client surface the provider actually uses."""

    def __init__(
        self,
        *,
        message_payloads: dict[str, dict[str, Any]] | None = None,
        list_pages: list[dict[str, Any]] | None = None,
        history_pages: list[Any] | None = None,
        labels: dict[str, Any] | None = None,
    ) -> None:
        self._payloads = message_payloads or {}
        self.list_pages = list_pages or [{}]
        self.history_pages = history_pages or [{}]
        self.labels_response = labels or LABELS_RESPONSE
        self.batch_errors: dict[str, Exception] = {}
        self.batches = 0
        self.list_calls = 0
        self.history_calls = 0

    def users(self) -> FakeService:
        return self

    # -- labels ---------------------------------------------------------
    def labels(self) -> FakeService:
        return self

    # -- messages -------------------------------------------------------
    def messages(self) -> FakeService:  # type: ignore[no-redef]
        return self

    def history(self) -> FakeService:
        return self

    def list(self, **kwargs: Any) -> _Request:
        if "startHistoryId" in kwargs:
            page = self.history_pages[self.history_calls]
            self.history_calls += 1
            return _Request(page)
        if "labelIds" in kwargs or "maxResults" in kwargs:
            index = self.list_calls
            self.list_calls += 1
            if index < len(self.list_pages):
                return _Request(self.list_pages[index])
            return _Request({})
        return _Request(self.labels_response)

    def get(self, **kwargs: Any) -> _Request:
        if "id" in kwargs and kwargs.get("format"):
            return _Request(self._payloads[kwargs["id"]])
        return _Request(self.labels_response)

    def new_batch_http_request(self, callback: Any) -> _Batch:
        return _Batch(self, callback)


def _provider(service: FakeService) -> GmailProvider:
    # A limiter with an enormous rate keeps tests from actually sleeping.
    return GmailProvider("test", service, limiter=TokenBucket(rate=10_000_000))


def test_capabilities_reflect_a_real_api() -> None:
    provider = _provider(FakeService())
    caps = provider.capabilities()
    assert Capability.INCREMENTAL_HISTORY in caps
    assert Capability.LABELS_MANY_TO_MANY in caps
    assert Capability.SERVER_SIDE_SEARCH in caps


def test_list_mailboxes() -> None:
    mailboxes = _provider(FakeService()).list_mailboxes()
    assert [m.name for m in mailboxes] == ["INBOX", "Grants"]


def test_full_sync_pages_and_batches() -> None:
    ids = [f"m{i}" for i in range(250)]
    messages = {i: {**SIMPLE_MESSAGE, "id": i, "historyId": "900"} for i in ids}
    service = FakeService(
        message_payloads=messages,
        list_pages=[
            {"messages": [{"id": i} for i in ids[:100]], "nextPageToken": "p2"},
            {"messages": [{"id": i} for i in ids[100:200]], "nextPageToken": "p3"},
            {"messages": [{"id": i} for i in ids[200:]]},
        ],
    )
    provider = GmailProvider(
        "test", service, limiter=TokenBucket(rate=10_000_000), batch_size=100
    )
    result = provider.sync(None)

    assert len(result.messages) == 250
    assert result.was_full_sync is True
    # 250 messages at 100 per batch, not 250 round trips.
    assert service.batches == 3


def test_full_sync_records_the_resume_point() -> None:
    service = FakeService(
        message_payloads={
            "m1": {**SIMPLE_MESSAGE, "id": "m1", "historyId": "500"},
            "m2": {**NEWSLETTER_MESSAGE, "id": "m2", "historyId": "900"},
        },
        list_pages=[{"messages": [{"id": "m1"}, {"id": "m2"}]}],
    )
    result = _provider(service).sync(None)
    assert result.cursor.token == "900"


def test_batch_failures_are_not_silent() -> None:
    """A partial fetch must not be reported as a complete sync."""
    service = FakeService(
        message_payloads={"m1": SIMPLE_MESSAGE, "m2": SIMPLE_MESSAGE},
        list_pages=[{"messages": [{"id": "m1"}, {"id": "m2"}]}],
    )
    service.batch_errors["m2"] = _FakeHttpError(500)
    with pytest.raises(ProviderError, match="1 of 2"):
        _provider(service).sync(None)


def test_incremental_sync_uses_history() -> None:
    from maildb.providers.base import SyncCursor

    service = FakeService(
        message_payloads={"msg-b": {**SIMPLE_MESSAGE, "id": "msg-b"}},
        history_pages=[
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "msg-b"}}]},
                    {"messagesDeleted": [{"message": {"id": "msg-a"}}]},
                ],
                "historyId": "999",
            }
        ],
    )
    result = _provider(service).sync(SyncCursor(token="900"))

    assert [m.provider_message_id for m in result.messages] == ["msg-b"]
    assert result.deleted_provider_ids == ["msg-a"]
    assert result.cursor.token == "999"
    assert result.was_full_sync is False


def test_expired_history_falls_back_to_a_full_sync() -> None:
    """Gmail keeps history about a week.

    A laptop closed over a holiday is an expected condition, not an error.
    """
    from maildb.providers.base import SyncCursor

    service = FakeService(
        message_payloads={"m1": SIMPLE_MESSAGE},
        list_pages=[{"messages": [{"id": "m1"}]}],
        history_pages=[_FakeHttpError(404)],
    )

    def raising_list(**kwargs: Any) -> _Request:
        if "startHistoryId" in kwargs:
            raise _FakeHttpError(404)
        return FakeService.list(service, **kwargs)

    service.list = raising_list  # type: ignore[method-assign]

    result = _provider(service).sync(SyncCursor(token="1"))
    assert result.was_full_sync is True
    assert len(result.messages) == 1


def test_other_http_errors_are_not_swallowed() -> None:
    """Only 404 means "history expired"; a 500 is a real failure."""
    from maildb.providers.base import SyncCursor

    service = FakeService()

    def raising_list(**kwargs: Any) -> _Request:
        raise _FakeHttpError(500)

    service.list = raising_list  # type: ignore[method-assign]

    with pytest.raises(_FakeHttpError):
        _provider(service).sync(SyncCursor(token="1"))


def _virtual_clock(rate: int) -> tuple[TokenBucket, list[float]]:
    """A bucket driven by a fake clock, so tests never really sleep."""
    ticks = [0.0]
    slept: list[float] = []

    bucket = TokenBucket(rate=rate, clock=lambda: ticks[0])

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        ticks[0] += seconds

    bucket._sleep = fake_sleep  # type: ignore[method-assign]
    return bucket, slept


def test_token_bucket_limits_sustained_rate() -> None:
    """The limiter is load-bearing during backfill, not decorative."""
    bucket, slept = _virtual_clock(100)
    for _ in range(30):
        bucket.consume(10)

    # 300 units at 100/second cannot complete without waiting.
    assert slept
    assert sum(slept) == pytest.approx(2.0, abs=0.2)


def test_a_request_larger_than_the_bucket_terminates() -> None:
    """One HTTP batch of 100 messages.get costs 500 units against a 250
    unit bucket. The bucket caps at its own rate, so this can never be
    satisfied in a single grant -- it must be drained in instalments
    rather than waited on forever."""
    bucket, slept = _virtual_clock(250)
    bucket.consume(500)
    assert sum(slept) == pytest.approx(1.0, abs=0.2)


def test_refill_rounding_cannot_stall_the_bucket() -> None:
    """Refilling for exactly the deficit can land a hair short, leaving a
    1e-15 deficit that asks for a 1e-17 second sleep and never
    progresses. Every wait has a floor."""
    bucket, slept = _virtual_clock(100)
    for _ in range(60):
        bucket.consume(7)
    assert all(s >= TokenBucket.MIN_DELAY for s in slept)
    assert sum(slept) == pytest.approx(3.2, abs=0.5)
