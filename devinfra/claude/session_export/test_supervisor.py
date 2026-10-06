from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from devinfra.claude.session_export.conftest import (
    LIVE_WINDOW,
    ONE,
    PAIRED_RESPONSE,
    SESSION_STATUS_ACTIVE,
    TEST_ORG_UUID,
    FakeSessionsService,
    FakeTokenEndpoint,
    authorization_state,
    background,
    eventually,
    make_event,
    make_events,
    redirect_url,
)
from devinfra.claude.session_export.oauth import CredentialStore
from devinfra.claude.session_export.store import SessionStore
from devinfra.claude.session_export.supervisor import LiveStatus, SyncState, SyncSupervisor


@dataclass
class Running:
    supervisor: SyncSupervisor
    credentials: CredentialStore
    token_endpoint: FakeTokenEndpoint

    async def approve(self) -> None:
        """What the human does: open the URL, approve, and paste back where the browser was sent."""
        await self.supervisor.finish_pairing(redirect_url(authorization_state(self.supervisor.start_pairing())))

    async def cycle_read(self, events: int) -> bool:
        cycle = (await self.supervisor.status()).last_cycle
        return cycle is not None and cycle.events_read == events


def supervisor_for(
    service: FakeSessionsService,
    store: SessionStore,
    path: Path,
    token_endpoint: FakeTokenEndpoint,
    *,
    live_streams: int = 0,
) -> Running:
    credentials = CredentialStore(path)
    supervisor = SyncSupervisor(
        credentials=credentials,
        store=store,
        token_client=token_endpoint.client,
        interval=3600,
        workers=2,
        live_streams=live_streams,
        live_window=LIVE_WINDOW,
        api_transport=httpx.MockTransport(service.handle),
    )
    return Running(supervisor, credentials, token_endpoint)


@pytest.fixture
async def running(
    service: FakeSessionsService, store: SessionStore, engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Running]:
    running = supervisor_for(service, store, tmp_path / "credential.json", FakeTokenEndpoint(PAIRED_RESPONSE))
    async with background(running.supervisor.run()):
        yield running


async def test_the_loop_waits_unpaired_and_syncs_once_a_redirect_is_pasted(
    service: FakeSessionsService, running: Running
) -> None:
    service.events = {ONE: make_events(3)}
    before = await running.supervisor.status()
    assert (before.state, before.credential, before.pairing_started) == (SyncState.UNPAIRED, None, False)

    running.supervisor.start_pairing()
    assert (await running.supervisor.status()).pairing_started
    await running.approve()
    await eventually(lambda: running.cycle_read(3))

    after = await running.supervisor.status()
    assert after.state is SyncState.IDLE
    assert (after.sessions, after.sessions_behind, after.pairing_started) == (1, 0, False)
    assert after.credential is not None
    assert (after.credential.organization_uuid, after.credential.scopes) == (TEST_ORG_UUID, ["user:profile"])


async def test_pairing_again_switches_the_loop_to_the_new_credential(
    service: FakeSessionsService, running: Running
) -> None:
    service.events = {ONE: make_events(3)}
    await running.approve()
    await eventually(lambda: running.cycle_read(3))

    running.token_endpoint.response = {**PAIRED_RESPONSE, "refresh_token": "test-refresh-2"}
    service.events[ONE].append(make_event(4))
    await running.approve()
    await eventually(lambda: running.cycle_read(1))  # the new run's first cycle reads only what is new

    assert running.credentials.load().refresh_token.get_secret_value() == "test-refresh-2"


async def test_a_failed_cycle_is_shown_and_the_next_success_clears_it(
    service: FakeSessionsService, running: Running
) -> None:
    service.events = {ONE: make_events(3)}
    service.fail_next = [401]
    await running.approve()

    async def failed() -> bool:
        return (await running.supervisor.status()).last_failure is not None

    await eventually(failed)
    status = await running.supervisor.status()
    assert status.last_failure is not None
    assert "HTTPStatusError" in status.last_failure.message
    assert (status.state, status.last_cycle) == (SyncState.IDLE, None)

    running.supervisor.sync_now()
    await eventually(lambda: running.cycle_read(3))
    assert (await running.supervisor.status()).last_failure is None


async def test_pasting_without_an_attempt_or_with_another_attempts_url_saves_nothing(
    service: FakeSessionsService, store: SessionStore, tmp_path: Path
) -> None:
    idle = supervisor_for(service, store, tmp_path / "credential.json", FakeTokenEndpoint(PAIRED_RESPONSE))
    with pytest.raises(ValueError, match="start one first"):
        await idle.supervisor.finish_pairing(redirect_url("any"))

    idle.supervisor.start_pairing()
    with pytest.raises(ValueError, match="state differs"):
        await idle.supervisor.finish_pairing(redirect_url("another-attempt"))
    with pytest.raises(ValueError, match="start one first"):  # the mismatch spent the attempt
        await idle.supervisor.finish_pairing(redirect_url("any"))

    assert not idle.credentials.exists()
    assert not idle.token_endpoint.bodies


async def test_a_code_anthropic_refuses_spends_the_attempt_and_saves_nothing(
    service: FakeSessionsService, store: SessionStore, tmp_path: Path
) -> None:
    refused = supervisor_for(service, store, tmp_path / "credential.json", FakeTokenEndpoint({}, status=400))
    with pytest.raises(httpx.HTTPStatusError):
        await refused.approve()
    assert not refused.credentials.exists()
    assert not (await refused.supervisor.status()).pairing_started


async def test_without_live_streams_the_sync_only_polls(service: FakeSessionsService, running: Running) -> None:
    service.events = {ONE: make_events(3)}
    service.statuses = {ONE: SESSION_STATUS_ACTIVE}
    await running.approve()
    await eventually(lambda: running.cycle_read(3))
    assert (await running.supervisor.status()).live == LiveStatus(
        following=False, watching=False, streams=0, last_event_at=None, problems=[], failure=None
    )
    assert service.streams == []


async def test_live_following_stores_a_pushed_event_and_reports_itself(
    service: FakeSessionsService, store: SessionStore, tmp_path: Path
) -> None:
    live = supervisor_for(
        service, store, tmp_path / "credential.json", FakeTokenEndpoint(PAIRED_RESPONSE), live_streams=1
    )
    async with background(live.supervisor.run()):
        service.events = {ONE: make_events(3)}
        service.statuses = {ONE: SESSION_STATUS_ACTIVE}
        await live.approve()

        async def stream_opened() -> bool:
            return bool(service.event_streams(ONE))

        await eventually(stream_opened)  # whichever of the first cycle and the follower stored the session first
        service.event_streams(ONE)[0].send("client_event", make_event(4), frame_id="4")

        async def stored_through_four() -> bool:
            return await store.resume_after(ONE) == 4

        await eventually(stored_through_four)
        status = (await live.supervisor.status()).live
        assert (status.following, status.streams, status.problems, status.failure) == (True, 1, [], None)
        assert status.last_event_at is not None


async def test_a_refused_stream_shows_on_the_page_with_its_reason(
    service: FakeSessionsService, store: SessionStore, tmp_path: Path
) -> None:
    live = supervisor_for(
        service, store, tmp_path / "credential.json", FakeTokenEndpoint(PAIRED_RESPONSE), live_streams=1
    )
    service.stream_refusals = [403] * 1000
    async with background(live.supervisor.run()):
        service.events = {ONE: make_events(3)}
        service.statuses = {ONE: SESSION_STATUS_ACTIVE}
        await live.approve()

        async def stream_reported() -> bool:  # the watch is refused too, and reported first
            return any(p.source == ONE for p in (await live.supervisor.status()).live.problems)

        await eventually(stream_reported)
        problem = {p.source: p for p in (await live.supervisor.status()).live.problems}[ONE]
        assert "403" in problem.message
        assert "/events/stream" in problem.message
        assert "refused" in problem.message


if __name__ == "__main__":
    pytest_bazel.main()
