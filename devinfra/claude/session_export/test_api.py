from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from pydantic import SecretStr
from tenacity import wait_none

from devinfra.claude.session_export.api import ResumePointLostError, SessionCookie, SessionsApi, StreamClosedEarlyError
from devinfra.claude.session_export.conftest import (
    ONE,
    RESUME_TOKEN,
    SESSION_STATUS_ACTIVE,
    TEST_ORG_UUID,
    FakeSessionsService,
    SseConnection,
    make_delivery_update,
    make_event,
    make_events,
)
from devinfra.claude.session_export.models import DeliveryUpdate, Event, SessionRemoved, SessionSummary
from devinfra.claude.session_export.oauth import CredentialStore, OAuthTokenSource


def test_cookie_file_accepts_a_full_header_and_hides_the_key(tmp_path: Path) -> None:
    header = tmp_path / "header.txt"
    header.write_text("a=1; sessionKey=test-key; lastActiveOrg=test-org; b=2\n")
    cookie = SessionCookie.from_file(header)
    assert (cookie.session_key.get_secret_value(), cookie.org_uuid) == ("test-key", "test-org")
    assert "test-key" not in repr(cookie)

    partial = tmp_path / "partial.txt"
    partial.write_text("sessionKey=test-key\n")
    with pytest.raises(ValueError, match="lastActiveOrg"):
        SessionCookie.from_file(partial)


async def test_list_sessions_follows_cursor_across_pages(service: FakeSessionsService, api: SessionsApi) -> None:
    service.events = {f"session_test{i:04d}": [] for i in range(250)}
    assert [s.id async for s in api.list_sessions()] == list(service.events)
    assert len(service.requests) == 3


@pytest.mark.parametrize(("count", "requests"), [(0, 1), (1, 1), (500, 1), (501, 2), (1203, 3)])
async def test_event_pages_cover_every_event_once_without_a_trailing_request(
    service: FakeSessionsService, api: SessionsApi, count: int, requests: int
) -> None:
    service.events[ONE] = make_events(count)
    seqs = [e.seq async for page in api.iter_event_pages(ONE) for e in page]
    assert seqs == list(range(1, count + 1))
    assert len(service.requests) == requests


@pytest.mark.parametrize(
    ("after", "missing", "pages_before_the_gap"),
    [
        pytest.param(0, 3, 0, id="first-page"),
        pytest.param(0, 501, 1, id="page-boundary"),
        pytest.param(0, 601, 1, id="later-page"),
        pytest.param(500, 501, 0, id="resumed-after"),
    ],
)
async def test_event_pages_reject_a_sequence_gap_before_yielding_its_page(
    service: FakeSessionsService, api: SessionsApi, after: int, missing: int, pages_before_the_gap: int
) -> None:
    service.events[ONE] = [e for e in make_events(1203) if int(e["sequence_num"]) != missing]
    pages = api.iter_event_pages(ONE, after=after)
    for _ in range(pages_before_the_gap):
        await anext(pages)
    with pytest.raises(ValueError, match=f"expected sequence_num {missing}, got {missing + 1}"):
        await anext(pages)


async def test_read_event_returns_the_event_with_the_stamps_it_has_now(
    service: FakeSessionsService, api: SessionsApi
) -> None:
    received = "2026-02-01T00:01:00+00:00"
    events = make_events(5)
    events[2] = make_event(3, received_at=received)
    service.events[ONE] = events
    event = await api.read_event(ONE, 3)
    assert (event.seq, event.received_at) == (3, received)
    [request] = service.requests
    assert dict(request.url.params) == {"limit": "1", "sort_order": "asc", "cursor": "2"}


async def test_read_event_refuses_an_event_the_route_does_not_have(
    service: FakeSessionsService, api: SessionsApi
) -> None:
    events = make_events(5)
    del events[2]
    service.events[ONE] = events
    with pytest.raises(ValueError, match="expected sequence_num 3, got 4"):
        await api.read_event(ONE, 3)


async def test_newest_sequence_num_is_zero_for_a_session_without_events(
    service: FakeSessionsService, api: SessionsApi
) -> None:
    service.events[ONE] = []
    assert await api.newest_sequence_num(ONE) == 0


async def test_transient_errors_are_retried_then_succeed(service: FakeSessionsService, api: SessionsApi) -> None:
    service.events[ONE] = make_events(3)
    service.fail_next = [503, 429]
    assert await api.newest_sequence_num(ONE) == 3
    assert len(service.requests) == 3


async def test_retries_are_bounded(service: FakeSessionsService, api: SessionsApi) -> None:
    service.fail_next = [503] * 20
    with pytest.raises(httpx.HTTPStatusError):
        await api.newest_sequence_num(ONE)
    assert len(service.requests) == 6


async def test_oauth_client_sends_its_bearer_to_the_first_party_api(
    service: FakeSessionsService, credential_store: Callable[..., CredentialStore]
) -> None:
    service.events[ONE] = make_events(3)
    async with httpx.AsyncClient() as token_client:
        api = SessionsApi.for_oauth(
            OAuthTokenSource(credential_store(), token_client),
            transport=httpx.MockTransport(service.handle),
            retry_wait=wait_none(),
        )
        async with api:
            assert await api.newest_sequence_num(ONE) == 3
    assert {request.url.host for request in service.requests} == {"api.anthropic.com"}


async def test_auth_failure_is_not_retried_and_carries_the_error_body(service: FakeSessionsService) -> None:
    wrong_key = SessionCookie(session_key=SecretStr("wrong-key"), org_uuid=TEST_ORG_UUID)
    transport = httpx.MockTransport(service.handle)
    async with SessionsApi.for_cookie(wrong_key, transport=transport, retry_wait=wait_none()) as api:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await api.newest_sequence_num(ONE)
    assert failure.value.response.status_code == 401
    assert any("authentication_error" in note for note in failure.value.__notes__)
    assert len(service.requests) == 1


async def test_recent_sessions_reads_one_page_of_the_newest(service: FakeSessionsService, api: SessionsApi) -> None:
    service.events = {f"session_test{i:04d}": [] for i in range(250)}
    assert [s.id for s in await api.recent_sessions(5)] == list(service.events)[:5]
    assert [r.url.params["limit"] for r in service.requests] == ["5"]

    service.requests.clear()
    assert len(await api.recent_sessions(500)) == 100  # the list route's page limit
    assert [r.url.params["limit"] for r in service.requests] == ["100"]


async def test_event_stream_yields_events_and_delivery_updates_and_skips_everything_else(
    service: FakeSessionsService, api: SessionsApi, caplog: pytest.LogCaptureFixture
) -> None:
    def script(stream: SseConnection) -> None:
        stream.send(None, {"keepalive": True}, frame_id="3")  # what the server sends as the stream opens
        stream.send("session_update", {"status": SESSION_STATUS_ACTIVE}, frame_id="3")
        stream.send("ephemeral_event", {"delta": "he"})
        stream.send("client_event", make_event(4), frame_id="4")
        stream.send("client_event", frame_id="5")  # advances the cursor only
        stream.send("delivery_update", make_delivery_update(4, "DELIVERY_STATUS_RECEIVED"))
        stream.send("client_event", make_event(5), frame_id="5")
        stream.close()

    service.on_open = [script]
    connected: list[str] = []
    items = [i async for i in api.stream_events(ONE, after=3, on_connected=lambda: connected.append("yes"))]
    assert [i.seq for i in items if isinstance(i, Event)] == [4, 5]
    assert [i.event_id for i in items if isinstance(i, DeliveryUpdate)] == [UUID(int=4)]
    assert connected == ["yes"]
    assert not caplog.records  # a keepalive and the known metadata frames are not worth a warning
    [opened] = service.event_streams(ONE)
    assert dict(opened.request.url.params) == {"from_sequence_num": "3"}
    assert opened.request.headers["last-event-id"] == "3"
    assert opened.request.url.path == "/v1/code/sessions/cse_test0001/events/stream"


async def test_an_unknown_frame_is_logged_once_by_shape_and_never_by_value(
    service: FakeSessionsService, api: SessionsApi, caplog: pytest.LogCaptureFixture
) -> None:
    def script(stream: SseConnection) -> None:
        stream.send("mystery", {"secret_key": "secret-value"})
        stream.send("mystery", {"secret_key": "another"})
        stream.close()

    service.on_open = [script]
    assert [e async for e in api.stream_events(ONE, after=3)] == []
    [record] = caplog.records
    assert "mystery" in record.message
    assert "['secret_key']" in record.message
    assert "secret-value" not in record.message


async def test_event_stream_from_the_start_sends_no_resume_position(
    service: FakeSessionsService, api: SessionsApi
) -> None:
    def script(stream: SseConnection) -> None:
        stream.send("client_event", make_event(1))
        stream.close()

    service.on_open = [script]
    assert [i.seq async for i in api.stream_events(ONE, after=0) if isinstance(i, Event)] == [1]
    [opened] = service.event_streams(ONE)
    assert not opened.request.url.params
    assert "last-event-id" not in opened.request.headers


async def test_event_stream_says_to_page_when_the_server_cannot_resume(
    service: FakeSessionsService, api: SessionsApi
) -> None:
    def truncate(stream: SseConnection) -> None:
        stream.send("catch_up_truncated")
        stream.close()

    service.on_open = [truncate]
    with pytest.raises(ResumePointLostError, match="truncated"):
        [e async for e in api.stream_events(ONE, after=3)]

    service.stream_refusals = [410]
    with pytest.raises(ResumePointLostError, match="410"):
        [e async for e in api.stream_events(ONE, after=3)]


async def test_event_stream_refusals_and_empty_closes_are_errors(
    service: FakeSessionsService, api: SessionsApi
) -> None:
    service.stream_refusals = [403]
    with pytest.raises(httpx.HTTPStatusError) as refused:
        [e async for e in api.stream_events(ONE, after=3)]
    assert any("refused" in note for note in refused.value.__notes__)

    service.on_open = [SseConnection.close]
    with pytest.raises(StreamClosedEarlyError):
        [e async for e in api.stream_events(ONE, after=3)]


async def test_resume_token_comes_from_a_one_item_list(service: FakeSessionsService, api: SessionsApi) -> None:
    service.events = {ONE: []}
    assert await api.resume_token() == RESUME_TOKEN
    assert service.requests[0].url.params["limit"] == "1"


async def test_watch_yields_session_changes(
    service: FakeSessionsService, api: SessionsApi, caplog: pytest.LogCaptureFixture
) -> None:
    service.events = {ONE: make_events(1)}
    item = service.list_item(ONE)

    def script(stream: SseConnection) -> None:
        stream.send(None, frame_id="cursor-0")  # a keepalive: no name, no data
        stream.send("sync", frame_id="cursor-1")
        stream.send("added", item, frame_id="cursor-2")
        stream.send("changed", {**item, "title": "Renamed"}, frame_id="cursor-3")
        stream.send("removed", {"id": item["id"]}, frame_id="cursor-4")
        stream.close()

    service.on_open = [script]
    connected: list[str] = []
    changes = [
        change async for change in api.watch_sessions(RESUME_TOKEN, on_connected=lambda: connected.append("yes"))
    ]
    assert changes == [
        SessionSummary(**item),
        SessionSummary(**{**item, "title": "Renamed"}),
        SessionRemoved(id=item["id"]),
    ]
    assert connected == ["yes"]
    assert not caplog.records  # the keepalive is not an unknown frame
    [opened] = service.watches()
    assert dict(opened.request.url.params) == {"exclude_tags": "-", "resume_token": RESUME_TOKEN}
    assert opened.request.headers["anthropic-client-platform"] == "web_claude_ai"


async def test_watch_says_when_the_server_no_longer_holds_the_token(
    service: FakeSessionsService, api: SessionsApi
) -> None:
    service.stream_refusals = [410]
    with pytest.raises(ResumePointLostError):
        [change async for change in api.watch_sessions("expired")]


async def test_a_watch_closed_without_a_frame_is_an_error(service: FakeSessionsService, api: SessionsApi) -> None:
    service.on_open = [SseConnection.close]
    with pytest.raises(StreamClosedEarlyError):
        [change async for change in api.watch_sessions(RESUME_TOKEN)]


if __name__ == "__main__":
    pytest_bazel.main()
