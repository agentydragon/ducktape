"""The app's Runner Gateway and History Service path against authenticated gRPC peers."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.live import LiveIndex
from agentplane.app.testing.dispatcher import runner_proxy_backed_dispatcher
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.view.content import ContentStore
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.testing.backend import ingest
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import command_pb2, runner_pb2, session_dispatcher_pb2
from agentplane.runner.runner import Runner
from agentplane.runner.testing import launches
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.runner_gateway.testing.runner_peer import runner_peer
from agentplane.sandbox_resource import SANDBOXES_PLURAL
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SandboxCluster, sandbox_cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//grpcio


@pytest.fixture
async def cluster() -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster() as started:
        yield started


@pytest.fixture
def register_attached_sessions() -> bool:
    # The runner pushes its history to the History Service ingress; the app only reads it.
    return True


@pytest.fixture(autouse=True)
async def discover(cluster: SandboxCluster, live_index: LiveIndex) -> None:
    live_index.sandboxes[SANDBOX] = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    live_index.pods[SANDBOX] = await k8s_client.CoreV1Api(cluster.api).read_namespaced_pod(SANDBOX, SANDBOX_NAMESPACE)


async def test_production_bridge_archives_native_evidence_across_bounded_service_copies(
    live_index: LiveIndex,
    tmp_path: Path,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    database_updates: WakeupListener,
    runner: Runner,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
    service_history_db_url: str,
) -> None:
    async with (
        runner_peer(tmp_path / "gateway-token", runner=runner) as endpoint,
        ingest(service_history_db_url, runner),
    ):
        remote = endpoint.client()
        directory = RunnerDirectory(live_index, remote)
        async with runner_proxy_backed_dispatcher(directory, event_logs, tmp_path / "dispatcher-token") as dispatcher:
            bridge = RunnerBridge(runners=directory, event_logs=event_logs, content=content, dispatcher=dispatcher)
            opening = await bridge.create_session(SANDBOX, uuid4(), spec)
            assert opening.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
            (runner_session,) = await directory.client(SANDBOX).list_sessions()
            assert runner_session.session_id == opening.session_id
            assert launches.PLATFORM_INSTRUCTIONS in runner_session.spec.instructions
            thread = UUID(opening.session_id)
            first = command_pb2.Command(command_id="remote-first", submit_input=command_pb2.SubmitInput(text="first"))
            receipt = (
                await dispatcher.unary(
                    dispatcher.stub.SubmitInput,
                    session_dispatcher_pb2.SubmitInputRequest(
                        command_id=first.command_id,
                        session_id=str(thread),
                        sandbox=SANDBOX,
                        text=first.submit_input.text,
                    ),
                )
            ).dispatch
            assert isinstance(receipt, session_dispatcher_pb2.Dispatch)
            assert receipt.admission.event.command_admitted.command == first
            # The runner receipt can arrive before the independent app archive copy.
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    assert receipt.admission in await event_logs.events(thread, limit=1000)
            await model.reply(await model.request(), Text("FIRST"))
            snapshot = await event_logs.feed_state(thread)
            assert snapshot is not None
            assert snapshot.end is None
            second = command_pb2.Command(
                command_id="remote-second", submit_input=command_pb2.SubmitInput(text="second")
            )
            next_receipt = (
                await dispatcher.unary(
                    dispatcher.stub.SubmitInput,
                    session_dispatcher_pb2.SubmitInputRequest(
                        command_id=second.command_id,
                        session_id=str(thread),
                        sandbox=SANDBOX,
                        text=second.submit_input.text,
                    ),
                )
            ).dispatch
            assert isinstance(next_receipt, session_dispatcher_pb2.Dispatch)
            assert next_receipt.admission.cursor > receipt.admission.cursor
            assert next_receipt.admission.origin.source_id == receipt.admission.origin.source_id
            await model.reply(await model.request(), Text("SECOND"))
            await dispatcher.unary(
                dispatcher.stub.StopRunnerSession,
                session_dispatcher_pb2.StopRunnerSessionRequest(
                    command_id="remote-stop", session_id=str(thread), sandbox=SANDBOX
                ),
            )
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(15), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    native = await runner.sessions[str(thread)].journal.since(0, limit=1000)
                    archived = await event_logs.events(thread, limit=1000)
                    assert archived == native
                    assert any(entry.event.HasField("harness_exited") for entry in archived)


async def test_history_resume_propagates_runner_not_found(
    live_index: LiveIndex,
    tmp_path: Path,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    runner: Runner,
    spec: runner_pb2.SessionSpec,
) -> None:
    async with runner_peer(tmp_path / "gateway-token", runner=runner) as endpoint:
        remote = endpoint.client()
        directory = RunnerDirectory(live_index, remote)
        async with runner_proxy_backed_dispatcher(directory, event_logs, tmp_path / "dispatcher-token") as dispatcher:
            bridge = RunnerBridge(runners=directory, event_logs=event_logs, content=content, dispatcher=dispatcher)
            opening = await bridge.create_session(SANDBOX, uuid4(), spec)
            thread_id = UUID(opening.session_id)

            # Gateway Go tests enforce the incarnation fence. This peer scripts NOT_FOUND so the
            # App test checks only that resume errors survive the client boundary.
            endpoint.peer.reject_next_list_sessions(grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")
            with pytest.raises(ServiceError) as refused:
                await bridge.resume_thread(thread_id, sandbox=SANDBOX, expected_harness=None, expected_cwd=None)
            assert refused.value.code == grpc.StatusCode.NOT_FOUND


async def test_runner_client_does_not_fall_back_after_peer_stops(
    live_index: LiveIndex, tmp_path: Path, runner: Runner
) -> None:
    async with runner_peer(tmp_path / "gateway-token", runner=runner) as endpoint:
        remote = endpoint.client()
        directory = RunnerDirectory(live_index, remote)
        assert await directory.client(SANDBOX).list_sessions() == []
    # Each call through the Sandbox destination dials the peer anew, here after it stopped.
    with pytest.raises(ServiceError):
        await directory.client(SANDBOX).list_sessions()
    assert not runner.sessions


async def test_runner_proxy_lists_and_resumes_a_stopped_session(
    live_index: LiveIndex, tmp_path: Path, runner: Runner, spec: runner_pb2.SessionSpec, model: ScriptedModel
) -> None:
    async with runner_peer(tmp_path / "gateway-token", runner=runner) as endpoint:
        remote = endpoint.client()
        session_id = str(uuid4())
        session = await runner.open_session(session_id, spec, None)
        await session.command(
            command_pb2.Command(command_id="resume-without-registry", submit_input=command_pb2.SubmitInput(text="hi"))
        )
        await model.reply(await model.request(), Text("hello"))
        directory = RunnerDirectory(live_index, remote)
        client = directory.client(SANDBOX)
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await client.list_sessions()
                assert summary.session_id == session_id
                assert not summary.active_turn_id
        await session.shutdown()
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await client.list_sessions()
                assert summary.harness_state == runner_pb2.HARNESS_STATE_STOPPED

        attached = await client.resume(session_id)
        assert attached.session_id == session_id
        assert attached.spec == summary.spec
        assert attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
        summaries = await client.list_sessions()
        assert [summary.session_id for summary in summaries] == [str(session_id)]


if __name__ == "__main__":
    pytest_bazel.main()
