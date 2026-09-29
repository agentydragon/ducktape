from pathlib import Path

import httpx
import pytest
import pytest_bazel
from pydantic import SecretStr
from tenacity import wait_none

from devinfra.claude.session_export.api import SessionCookie, SessionsApi
from devinfra.claude.session_export.conftest import TEST_COOKIE, FakeSessionsService, make_events

SESSION_ID = "session_test0001"


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
    service.events[SESSION_ID] = make_events(count)
    seqs = [e.seq async for page in api.iter_event_pages(SESSION_ID) for e in page]
    assert seqs == list(range(1, count + 1))
    assert len(service.requests) == requests


async def test_newest_sequence_num_is_zero_for_a_session_without_events(
    service: FakeSessionsService, api: SessionsApi
) -> None:
    service.events[SESSION_ID] = []
    assert await api.newest_sequence_num(SESSION_ID) == 0


async def test_transient_errors_are_retried_then_succeed(service: FakeSessionsService, api: SessionsApi) -> None:
    service.events[SESSION_ID] = make_events(3)
    service.fail_next = [503, 429]
    assert await api.newest_sequence_num(SESSION_ID) == 3
    assert len(service.requests) == 3


async def test_retries_are_bounded(service: FakeSessionsService, api: SessionsApi) -> None:
    service.fail_next = [503] * 20
    with pytest.raises(httpx.HTTPStatusError):
        await api.newest_sequence_num(SESSION_ID)
    assert len(service.requests) == 6


async def test_auth_failure_is_not_retried_and_carries_the_error_body(service: FakeSessionsService) -> None:
    wrong_key = SessionCookie(session_key=SecretStr("wrong-key"), org_uuid=TEST_COOKIE.org_uuid)
    async with SessionsApi(wrong_key, transport=httpx.MockTransport(service.handle), retry_wait=wait_none()) as api:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await api.newest_sequence_num(SESSION_ID)
    assert failure.value.response.status_code == 401
    assert any("authentication_error" in note for note in failure.value.__notes__)
    assert len(service.requests) == 1


if __name__ == "__main__":
    pytest_bazel.main()
