"""The app's Runner Gateway and History Service path against authenticated gRPC peers."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

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
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import command_pb2, runner_pb2
from agentplane.runner import runner_service_pb2
from agentplane.runner.testing import launches
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
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
    runner: RunningRunner,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
    history_service: GoHistoryService,
) -> None:
    async with runner_peer(tmp_path / "gateway-token", runner=runner) as endpoint, history_service.ingest(runner):
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
            runner_client = directory.client(SANDBOX)
            status = await runner_client.status()
            assert not status.processes
            (dormant,) = [row for row in status.sessions if row.session_id == str(thread)]
            assert dormant.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT
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
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    first_turn = runner.entries(str(thread))
                    assert any(entry.event.WhichOneof("observation") == "turn_completed" for entry in first_turn)
            snapshot = await event_logs.feed_state(thread)
            assert snapshot is not None
            assert snapshot.end is None
            active = await runner_client.status()
            (active_session,) = [row for row in active.sessions if row.session_id == str(thread)]
            native_session_id = active_session.native_session_id
            assert native_session_id
            assert len(active.processes) == 1
            starts = [entry for entry in first_turn if entry.event.WhichOneof("observation") == "harness_started"]
            assert starts[-1].event.harness_started.resumed is False
            await runner.restart()
            stopped = await runner_client.status()
            assert not stopped.processes
            (stopped_session,) = [row for row in stopped.sessions if row.session_id == str(thread)]
            assert stopped_session.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT
            assert stopped_session.native_session_id == native_session_id
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
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    native = runner.entries(str(thread))
                    starts = [entry for entry in native if entry.event.WhichOneof("observation") == "harness_started"]
                    assert len(starts) == 2
                    assert starts[0].event.harness_started.resumed is False
                    assert starts[1].event.harness_started.resumed is True
                    second_turn = [
                        entry
                        for entry in native
                        if entry.event.WhichOneof("observation") == "turn_completed"
                        and entry.cursor > next_receipt.admission.cursor
                    ]
                    assert second_turn
            resumed = await runner_client.status()
            (resumed_session,) = [row for row in resumed.sessions if row.session_id == str(thread)]
            assert resumed_session.native_session_id == native_session_id
            assert len(resumed.processes) == 1
            await runner.restart()
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(15), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    native = runner.entries(str(thread))
                    archived = await event_logs.events(thread, limit=1000)
                    assert archived == native
                    assert any(entry.event.HasField("harness_exited") for entry in archived)


async def test_runner_client_does_not_fall_back_after_peer_stops(live_index: LiveIndex, tmp_path: Path) -> None:
    async with runner_peer(tmp_path / "gateway-token") as endpoint:
        remote = endpoint.client()
        directory = RunnerDirectory(live_index, remote)
        assert await directory.client(SANDBOX).list_sessions() == []
    # Each call through the Sandbox destination dials the peer anew, here after it stopped.
    with pytest.raises(ServiceError):
        await directory.client(SANDBOX).list_sessions()


async def test_runner_proxy_lists_and_sends_to_a_dormant_session(
    live_index: LiveIndex,
    tmp_path: Path,
    runner: RunningRunner,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
    event_logs: SeededEventLogStore,
) -> None:
    async with runner_peer(tmp_path / "gateway-token", runner=runner) as endpoint:
        remote = endpoint.client()
        session_id = str(uuid4())
        await (await runner.control()).OpenSession(
            runner_service_pb2.OpenSessionRequest(session_id=session_id, spec=spec)
        )
        await (await runner.control()).SubmitInput(
            runner_service_pb2.SubmitInputRequest(session_id=session_id, command_id="first", text="hi")
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
        status = await client.status()
        (active,) = [row for row in status.sessions if row.session_id == session_id]
        native_session_id = active.native_session_id
        assert native_session_id
        assert len(status.processes) == 1
        # This exercises orderly runner shutdown and recovery. Killing the harness
        # directly is a process-loss failure, which is reported as FAILED.
        await runner.restart()
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await client.list_sessions()
                assert summary.harness_state == runner_pb2.HARNESS_STATE_STOPPED
        status = await client.status()
        assert not status.processes
        (dormant,) = [row for row in status.sessions if row.session_id == session_id]
        assert dormant.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT
        assert dormant.native_session_id == native_session_id

        async with runner_proxy_backed_dispatcher(directory, event_logs, tmp_path / "dispatcher-token") as dispatcher:
            receipt = await dispatcher.unary(
                dispatcher.stub.SubmitInput,
                session_dispatcher_pb2.SubmitInputRequest(
                    session_id=session_id, command_id="wake-on-input", sandbox=SANDBOX, text="again"
                ),
            )
            assert receipt.dispatch.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
            assert receipt.dispatch.admission.event.command_admitted.command.command_id == "wake-on-input"
            await model.reply(await model.request(), Text("resumed"))
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    entries = runner.entries(session_id)
                    starts = [entry for entry in entries if entry.event.WhichOneof("observation") == "harness_started"]
                    assert len(starts) == 2
                    assert starts[0].event.harness_started.resumed is False
                    assert starts[1].event.harness_started.resumed is True
                    assert any(
                        entry.event.WhichOneof("observation") == "turn_completed"
                        and entry.cursor > receipt.dispatch.admission.cursor
                        for entry in entries
                    )
            status = await client.status()
            (resumed,) = [row for row in status.sessions if row.session_id == session_id]
            assert resumed.native_session_id == native_session_id
            assert len(status.processes) == 1
        summaries = await client.list_sessions()
        assert [summary.session_id for summary in summaries] == [str(session_id)]


if __name__ == "__main__":
    pytest_bazel.main()
