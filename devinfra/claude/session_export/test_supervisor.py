import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from devinfra.claude.session_export.conftest import (
    PAIRED_RESPONSE,
    FakeSessionsService,
    FakeTokenEndpoint,
    eventually,
    make_event,
    make_events,
)
from devinfra.claude.session_export.oauth import CredentialStore
from devinfra.claude.session_export.store import SessionStore
from devinfra.claude.session_export.supervisor import SyncState, SyncSupervisor

ONE = "session_test0001"


@dataclass
class Running:
    supervisor: SyncSupervisor
    credentials: CredentialStore
    token_endpoint: FakeTokenEndpoint

    async def approve(self) -> None:
        """What the human does: open the URL, approve, and paste back where the browser was sent."""
        state = parse_qs(urlsplit(self.supervisor.start_pairing()).query)["state"][0]
        await self.supervisor.finish_pairing(f"http://localhost:54545/callback?code=test-code&state={state}")

    async def cycle_read(self, events: int) -> bool:
        cycle = (await self.supervisor.status()).last_cycle
        return cycle is not None and cycle.events_read == events


def supervisor_for(
    service: FakeSessionsService, store: SessionStore, path: Path, token_endpoint: FakeTokenEndpoint
) -> Running:
    credentials = CredentialStore(path)
    supervisor = SyncSupervisor(
        credentials=credentials,
        store=store,
        token_client=token_endpoint.client,
        interval=3600,
        workers=2,
        api_transport=httpx.MockTransport(service.handle),
    )
    return Running(supervisor, credentials, token_endpoint)


@pytest.fixture
async def running(
    service: FakeSessionsService, store: SessionStore, engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Running]:
    running = supervisor_for(service, store, tmp_path / "credential.json", FakeTokenEndpoint(PAIRED_RESPONSE))
    loop = asyncio.create_task(running.supervisor.run())
    yield running
    loop.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop


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
    assert (after.credential.organization_uuid, after.credential.scopes) == ("test-org-uuid", ["user:profile"])


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
        await idle.supervisor.finish_pairing("http://localhost:54545/callback?code=test-code&state=any")

    idle.supervisor.start_pairing()
    with pytest.raises(ValueError, match="state differs"):
        await idle.supervisor.finish_pairing("http://localhost:54545/callback?code=test-code&state=another-attempt")
    with pytest.raises(ValueError, match="start one first"):  # the mismatch spent the attempt
        await idle.supervisor.finish_pairing("http://localhost:54545/callback?code=test-code&state=any")

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


if __name__ == "__main__":
    pytest_bazel.main()
