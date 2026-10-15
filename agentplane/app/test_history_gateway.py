"""The app's Runner Gateway and History Service path against authenticated gRPC peers."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.live import LiveIndex
from agentplane.app.testing.dispatcher import GatewayBackedDispatcher
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.view.content import ContentStore
from agentplane.grpc_calls import ServiceError
from agentplane.history_service.testing.backend import ingest
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import command_pb2, runner_pb2, session_dispatcher_pb2
from agentplane.runner.runner import Runner
from agentplane.runner.testing import launches
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.runner_gateway.testing.connected_runner import authenticated_runner_gateway
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
    cluster: SandboxCluster,
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
        authenticated_runner_gateway(
            cluster, runner, tmp_path / "gateway-token", database_url=service_history_db_url
        ) as remote,
        ingest(service_history_db_url, runner),
    ):
        directory = RunnerDirectory(live_index, remote)
        bridge = RunnerBridge(
            runners=directory,
            event_logs=event_logs,
            content=content,
            dispatcher=GatewayBackedDispatcher(directory, event_logs),
        )
        opened = await bridge.create_session(SANDBOX, uuid4(), spec)
        assert launches.PLATFORM_INSTRUCTIONS in opened.attached.spec.instructions
        thread = UUID(opened.session_id)
        first = command_pb2.Command(command_id="remote-first", submit_input=command_pb2.SubmitInput(text="first"))
        receipt = await bridge.command(thread, first)
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
        second = command_pb2.Command(command_id="remote-second", submit_input=command_pb2.SubmitInput(text="second"))
        next_receipt = await bridge.command(thread, second)
        assert isinstance(next_receipt, session_dispatcher_pb2.Dispatch)
        assert next_receipt.admission.cursor > receipt.admission.cursor
        assert next_receipt.admission.origin.source_id == receipt.admission.origin.source_id
        await model.reply(await model.request(), Text("SECOND"))
        await bridge.command(
            thread, command_pb2.Command(command_id="remote-stop", stop_runner_session=command_pb2.StopRunnerSession())
        )
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(15), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                native = await runner.sessions[str(thread)].journal.since(0, limit=1000)
                archived = await event_logs.events(thread, limit=1000)
                assert archived == native
                assert any(entry.event.HasField("harness_exited") for entry in archived)


async def test_stopped_service_never_falls_back_to_reachable_runner(
    cluster: SandboxCluster, live_index: LiveIndex, tmp_path: Path, runner: Runner, service_history_db_url: str
) -> None:
    async with authenticated_runner_gateway(
        cluster, runner, tmp_path / "gateway-token", database_url=service_history_db_url
    ) as remote:
        directory = RunnerDirectory(live_index, remote)
        assert await directory.client(SANDBOX).list_sessions() == []
    # Each call to the Sandbox's runner dials its control Service anew, here with the gateway gone.
    with pytest.raises(ServiceError):
        await directory.client(SANDBOX).list_sessions()
    assert not runner.sessions


if __name__ == "__main__":
    pytest_bazel.main()
