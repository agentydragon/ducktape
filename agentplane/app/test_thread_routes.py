"""Focused contracts for Session creation and History-backed Thread routes."""

from __future__ import annotations

from uuid import uuid4

import httpx
from google.protobuf.json_format import MessageToDict
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.proto import session_pb2
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SandboxCluster
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient, accepted_session

# These plugins provide the real App, Dispatcher, History, Sandbox, and Go Runtime fixtures.
# gazelle:include_dep //agentplane/app:integration_fixtures
# gazelle:include_dep //agentplane/app/testing:runtime_fixtures
pytest_plugins = ("agentplane.app.integration_fixtures", "agentplane.app.testing.runtime_fixtures")

SESSIONS = f"/sandboxes/{SANDBOX}/sessions"


async def test_history_openings_do_not_create_app_thread_metadata(
    sandbox: SandboxCluster,
    session_dispatcher: SessionDispatcherClient,
    runner: RunningRunner,
    http: httpx.AsyncClient,
    spec: session_pb2.SessionSpec,
) -> None:
    # No app HTTP request created this Session; History owns its record independently of App metadata.
    session_id = uuid4()
    exact_spec = session_pb2.SessionSpec()
    exact_spec.CopyFrom(spec)
    exact_spec.placement.environment_hosted.environment.name = SANDBOX
    exact_spec.placement.environment_hosted.environment.uid = sandbox.destination.target.uid
    await session_dispatcher.unary(
        session_dispatcher.stub.CreateSession,
        session_dispatcher_pb2.CreateSessionRequest(session_id=str(session_id), spec=exact_spec),
    )
    await accepted_session(session_dispatcher, session_id)
    # History records the Session, while the App has no metadata row for this History-only Session.
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            summary = await http.get(f"/upstream/history/v1/sessions/{session_id}/summary")
            assert summary.status_code == 200, summary.text
            assert summary.json()["sandbox"]["name"] == SANDBOX
    assert (await http.get(f"/upstream/app/v1/threads/{session_id}")).status_code == 404
    client = Client(http)
    view = await client.wait_for_thread_projection(session_id, timeout_s=10)
    assert view.id == session_id
    assert view.name is None
    assert not view.archived
    assert session_id in {thread.id async for thread in client.threads(sandbox_uid=sandbox.destination.target.uid)}
    # Client-side defaults must not create an App metadata row.
    assert (await http.get(f"/upstream/app/v1/threads/{session_id}")).status_code == 404


async def test_app_session_route_records_the_exact_dispatcher_session_and_thread(
    sandbox: SandboxCluster,
    session_dispatcher: SessionDispatcherClient,
    runner: RunningRunner,
    http: httpx.AsyncClient,
    spec: session_pb2.SessionSpec,
) -> None:
    session_id = uuid4()
    sandbox_uid = sandbox.destination.target.uid
    exact_spec = session_pb2.SessionSpec()
    exact_spec.CopyFrom(spec)
    exact_spec.placement.environment_hosted.environment.name = SANDBOX
    exact_spec.placement.environment_hosted.environment.uid = sandbox_uid

    opened = await http.post(
        SESSIONS, json={"session_id": str(session_id), "sandbox_uid": sandbox_uid, "spec": MessageToDict(exact_spec)}
    )
    assert opened.status_code == 201, opened.text
    assert opened.json()["sessionId"] == str(session_id)

    await accepted_session(session_dispatcher, session_id)
    accepted = await session_dispatcher.get_session(str(session_id))
    assert accepted.session_id == str(session_id)
    assert accepted.status.materialization == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED
    assert accepted.placement == exact_spec.placement

    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            metadata = await http.get(f"/upstream/app/v1/threads/{session_id}")
            assert metadata.status_code == 200, metadata.text
            assert metadata.json()["threadId"] == str(session_id)

            summary = await http.get(f"/upstream/history/v1/sessions/{session_id}/summary")
            assert summary.status_code == 200, summary.text
            assert summary.json()["sessionId"] == str(session_id)
            assert summary.json()["sandbox"] == {"namespace": sandbox.namespace, "name": SANDBOX, "uid": sandbox_uid}
