"""The production app directory/bridge/archive against authenticated Sandbox Service gRPC."""

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.database_updates import Channel, DatabaseUpdates
from agentplane.app.live import LiveIndex
from agentplane.app.threads import ingestion as ingestion_module
from agentplane.app.threads.bridge import MalformedMessageError, RunnerBridge
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.ingestion import Feed, Ingester, Ingestion
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.view.content import ContentStore
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.testing.fixtures import RunnerHandle
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.sandbox_service.client import Attachment, ReconnectRequiredError, ServiceError
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


@pytest.fixture(autouse=True)
async def discover(cluster: Cluster, live_index: LiveIndex) -> None:
    live_index.sandboxes[SANDBOX] = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    live_index.pods[SANDBOX] = await k8s_client.CoreV1Api(cluster.api).read_namespaced_pod(SANDBOX, SANDBOX_NAMESPACE)


@pytest.mark.parametrize("lost_marker", [False, True])
async def test_production_bridge_archives_native_evidence_across_service_leases(
    cluster: Cluster,
    live_index: LiveIndex,
    tmp_path: Path,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    content: ContentStore,
    database_updates: DatabaseUpdates,
    runner: RunnerHandle,
    spec: protocol_pb2.SessionSpec,
    model: ScriptedModel,
    lost_marker: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="agentplane.app.threads.ingestion")
    if lost_marker:
        next_entry = Attachment.next_entry

        async def lose_marker(attachment: Attachment) -> event_log_pb2.EventEntry:
            try:
                return await next_entry(attachment)
            except ReconnectRequiredError as error:
                raise ConnectionError("lost terminal observation") from error

        monkeypatch.setattr(Attachment, "next_entry", lose_marker)
    async with authenticated_service(cluster, runner.port, tmp_path / "service-token") as remote:
        directory = SandboxSessions(live_index, remote)
        ingester = Ingester(runners=directory, event_logs=event_logs, ingestion=ingestion)
        bridge = RunnerBridge(
            runners=directory,
            event_logs=event_logs,
            content=content,
            ingester=ingester,
            thread_changes=database_updates.changes[Channel.THREADS],
        )
        try:
            with pytest.raises(MalformedMessageError):
                await bridge.open_session(
                    SANDBOX, "invalid-overrides", {"reasoning_effort": "low", "reasoningEffort": "high"}
                )
            assert "invalid-overrides" not in runner.runner.sessions
            opened = await bridge.open_session(SANDBOX, "remote-app", spec)
            assert "Backend guidance" in opened.spec.instructions
            thread = await event_logs.find(SANDBOX, "remote-app")
            assert thread is not None
            first = command_pb2.Command(command_id="remote-first", submit_input=command_pb2.SubmitInput(text="first"))
            receipt = await bridge.command(thread, first)
            assert receipt.event.command_admitted.command == first
            assert receipt in await event_logs.events(thread, limit=1000)
            await model.reply(await model.request(), Text("FIRST"))
            # Observe actual renewal (or lost-marker recovery), not just elapsed wall time.
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    notice = "reconnecting ingestion" if lost_marker else "renewing ingestion"
                    assert any(notice in record.message for record in caplog.records)
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
                    native = await runner.runner.sessions["remote-app"].journal.since(0, limit=1000)
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
            await directory.close()


@pytest.mark.parametrize(
    ("failures", "code", "warns"),
    [
        (1, grpc.StatusCode.UNAVAILABLE, False),
        (5, grpc.StatusCode.UNAVAILABLE, True),
        (1, grpc.StatusCode.PERMISSION_DENIED, True),
    ],
)
async def test_reconnect_diagnostics(
    cluster: Cluster,
    live_index: LiveIndex,
    tmp_path: Path,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failures: int,
    code: grpc.StatusCode,
    warns: bool,
) -> None:
    monkeypatch.setattr(ingestion_module, "RECONCILE_S", 0.01)
    monkeypatch.setattr(ingestion_module, "RECONNECT_WARNING_S", 0.025)
    attempts = 0

    async def copy(feed: Feed) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise ServiceError(code)

    monkeypatch.setattr(Feed, "_copy", copy)
    async with authenticated_service(cluster, 1, tmp_path / "service-token") as remote:
        directory = SandboxSessions(live_index, remote)
        lease = await ingestion.acquire(SANDBOX, timedelta(seconds=30))
        assert lease is not None
        try:
            await Feed(
                session_id="retry",
                client=directory.client(SANDBOX),
                event_logs=event_logs,
                ingestion=ingestion,
                lease=lease,
            ).run()
            assert attempts == failures + 1
            warnings = [record for record in caplog.records if "ingestion reconnect unsuccessful" in record.message]
            assert bool(warnings) == warns
            for record in warnings:
                assert "retrying for" in record.message
                assert record.exc_info is not None
                assert isinstance(record.exc_info[1], ServiceError)
                assert record.exc_info[1].code == code
        finally:
            await ingestion.release(lease)
            await directory.close()


async def test_stopped_service_never_falls_back_to_reachable_runner(
    cluster: Cluster, live_index: LiveIndex, tmp_path: Path, runner: RunnerHandle
) -> None:
    async with authenticated_service(cluster, runner.port, tmp_path / "service-token") as remote:
        directory = SandboxSessions(live_index, remote)
        assert await directory.client(SANDBOX).list_sessions() == []
    try:
        with pytest.raises(ServiceError):
            await directory.client(SANDBOX).list_sessions()
        assert not runner.runner.sessions
    finally:
        await directory.close()


if __name__ == "__main__":
    pytest_bazel.main()
