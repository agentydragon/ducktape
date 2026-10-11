"""The production app directory/bridge/archive against authenticated Sandbox Service and History Service gRPC."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.live import LiveIndex
from agentplane.app.testing.history import SeededEventLogStore as EventLogStore
from agentplane.app.threads.bridge import MalformedMessageError, RunnerBridge
from agentplane.app.threads.ingestion import Ingester
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.view.content import ContentStore
from agentplane.history_service.testing.backend import ingest
from agentplane.postgres.wakeups import Wakeups
from agentplane.proto import command_pb2, runner_pb2
from agentplane.runner.testing.fixtures import RunnerHandle
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.sandbox_service.client import ServiceError
from agentplane.sandbox_service.testing.kubernetes import SANDBOX, Cluster, authenticated_service, kubernetes
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from util.agent_sandbox import SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//grpcio


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    async with kubernetes() as started:
        yield started


@pytest.fixture
def register_attached_sessions() -> bool:
    # The History Service ingester writes the history; the app only reads it.
    return True


@pytest.fixture(autouse=True)
async def discover(cluster: Cluster, live_index: LiveIndex) -> None:
    live_index.sandboxes[SANDBOX] = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    live_index.pods[SANDBOX] = await k8s_client.CoreV1Api(cluster.api).read_namespaced_pod(SANDBOX, SANDBOX_NAMESPACE)


async def test_production_bridge_archives_native_evidence_across_bounded_service_copies(
    cluster: Cluster,
    live_index: LiveIndex,
    tmp_path: Path,
    event_logs: EventLogStore,
    content: ContentStore,
    database_updates: Wakeups,
    runner: RunnerHandle,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
    caplog: pytest.LogCaptureFixture,
    service_history_db_url: str,
) -> None:
    async with (
        authenticated_service(
            cluster, runner.port, tmp_path / "service-token", database_url=service_history_db_url
        ) as remote,
        ingest(service_history_db_url, remote),
    ):
        directory = SandboxSessions(live_index, remote)
        ingester = Ingester(runners=directory, event_logs=event_logs)
        bridge = RunnerBridge(runners=directory, event_logs=event_logs, content=content, ingester=ingester)
        try:
            with pytest.raises(MalformedMessageError):
                await bridge.create_session(SANDBOX, uuid4(), {"reasoning_effort": "low", "reasoningEffort": "high"})
            assert not runner.runner.sessions
            opened = await bridge.create_session(SANDBOX, uuid4(), MessageToDict(spec))
            assert "Backend guidance" in opened.spec.instructions
            thread = UUID(opened.session_id)
            first = command_pb2.Command(command_id="remote-first", submit_input=command_pb2.SubmitInput(text="first"))
            receipt = await bridge.command(thread, first)
            assert receipt.event.command_admitted.command == first
            # The runner receipt can arrive before the independent app archive copy.
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    assert receipt in await event_logs.events(thread, limit=1000)
            await model.reply(await model.request(), Text("FIRST"))
            snapshot = await event_logs.feed_state(thread)
            assert snapshot is not None
            assert snapshot.end is None
            second = command_pb2.Command(
                command_id="remote-second", submit_input=command_pb2.SubmitInput(text="second")
            )
            next_receipt = await bridge.command(thread, second)
            assert next_receipt.cursor > receipt.cursor
            assert next_receipt.origin.source_id == receipt.origin.source_id
            await model.reply(await model.request(), Text("SECOND"))
            await bridge.command(
                thread,
                command_pb2.Command(command_id="remote-stop", stop_runner_session=command_pb2.StopRunnerSession()),
            )
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(15), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    native = await runner.runner.sessions[str(thread)].journal.since(0, limit=1000)
                    archived = await event_logs.events(thread, limit=1000)
                    assert archived == native
                    assert any(entry.event.HasField("harness_exited") for entry in archived)
            assert not [
                record
                for record in caplog.records
                if record.name == "agentplane.app.threads.ingestion" and record.levelno >= logging.WARNING
            ]
        finally:
            await ingester.close()


async def test_stopped_service_never_falls_back_to_reachable_runner(
    cluster: Cluster, live_index: LiveIndex, tmp_path: Path, runner: RunnerHandle, service_history_db_url: str
) -> None:
    async with authenticated_service(
        cluster, runner.port, tmp_path / "service-token", database_url=service_history_db_url
    ) as remote:
        directory = SandboxSessions(live_index, remote)
        assert await directory.client(SANDBOX).list_sessions() == []
    async with remote:
        with pytest.raises(ServiceError):
            await directory.client(SANDBOX).list_sessions()
        assert not runner.runner.sessions


if __name__ == "__main__":
    pytest_bazel.main()
