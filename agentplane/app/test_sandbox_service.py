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

from agentplane.app.database_updates import DatabaseUpdates
from agentplane.app.live import LiveIndex
from agentplane.app.testing.history import (
    ProjectedHistory as Ingestion,
    ProjectedIngester as Ingester,
    SeededEventLogStore as EventLogStore,
)
from agentplane.app.threads.bridge import MalformedMessageError, RunnerBridge
from agentplane.app.threads.events.debug import ThreadScopeChangedError
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import AppSummaries, FeedEnd, HistorySummaries
from agentplane.app.threads.thread_view import ThreadView
from agentplane.app.threads.view.content import ContentStore, HistoryContentStore
from agentplane.history_service.testing.backend import fold_history, history_service, ingest
from agentplane.proto import command_pb2, runner_pb2
from agentplane.runner.testing.fixtures import RunnerHandle
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.sandbox_service.client import ServiceError
from agentplane.sandbox_service.testing.kubernetes import SANDBOX, Cluster, authenticated_service, kubernetes
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE
from agentplane.thread_fold.views import EntityKind
from util.agent_sandbox import SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//grpcio


def _thread_view_differences(history: list[ThreadView], app: list[ThreadView]) -> str:
    """Show every field that differs when the two projections disagree."""
    history_by_id = {view.id: view.model_dump(mode="python") for view in history}
    app_by_id = {view.id: view.model_dump(mode="python") for view in app}
    history_ids = [view.id for view in history]
    app_ids = [view.id for view in app]
    differences = [f"thread order: history={history_ids!r}, app={app_ids!r}"] if history_ids != app_ids else []

    for thread_id in history_ids:
        if thread_id not in app_by_id:
            differences.append(f"{thread_id}: present in history projection only")
            continue
        history_fields = history_by_id[thread_id]
        app_fields = app_by_id[thread_id]
        differences.extend(
            f"{thread_id}.{field}: history={history_fields[field]!r}, app={app_fields[field]!r}"
            for field in history_fields
            if history_fields[field] != app_fields[field]
        )
    differences.extend(
        f"{thread_id}: present in app projection only" for thread_id in app_ids if thread_id not in history_by_id
    )

    return "ThreadView projections differ:\n" + "\n".join(differences)


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    async with kubernetes() as started:
        yield started


@pytest.fixture(autouse=True)
async def discover(cluster: Cluster, live_index: LiveIndex) -> None:
    live_index.sandboxes[SANDBOX] = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    live_index.pods[SANDBOX] = await k8s_client.CoreV1Api(cluster.api).read_namespaced_pod(SANDBOX, SANDBOX_NAMESPACE)


async def test_production_bridge_archives_native_evidence_across_bounded_service_copies(
    cluster: Cluster,
    live_index: LiveIndex,
    tmp_path: Path,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    content: ContentStore,
    database_updates: DatabaseUpdates,
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
        history_service(service_history_db_url, tmp_path / "history-service-token") as history,
    ):
        event_logs = EventLogStore(event_logs.engine, peer=event_logs.peer, history_reader=history)
        ingestion = Ingestion(event_logs.engine, peer=event_logs.peer, history_reader=history)
        content = ContentStore(event_logs.engine, history_reader=history)
        directory = SandboxSessions(live_index, remote)
        ingester = Ingester(runners=directory, event_logs=event_logs, ingestion=ingestion)
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
            await directory.close()


async def test_history_service_reads_match_the_app_projection(
    cluster: Cluster,
    live_index: LiveIndex,
    tmp_path: Path,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    content: ContentStore,
    runner: RunnerHandle,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
    service_history_db_url: str,
) -> None:
    """Both folds over one retained history: every Thread read must agree but the epoch it names."""
    engine = event_logs.engine
    app = AppSummaries(engine)
    async with (
        authenticated_service(
            cluster, runner.port, tmp_path / "service-token", database_url=service_history_db_url
        ) as remote,
        ingest(service_history_db_url, remote),
        history_service(service_history_db_url, tmp_path / "history-service-token") as history,
    ):
        event_logs = EventLogStore(engine, peer=event_logs.peer, history_reader=history)
        ingestion = Ingestion(engine, peer=event_logs.peer, history_reader=history)
        content = ContentStore(engine, history_reader=history)
        directory = SandboxSessions(live_index, remote)
        ingester = Ingester(runners=directory, event_logs=event_logs, ingestion=ingestion)
        bridge = RunnerBridge(runners=directory, event_logs=event_logs, content=content, ingester=ingester)
        try:
            thread = UUID((await bridge.create_session(SANDBOX, uuid4(), MessageToDict(spec))).session_id)
            command = command_pb2.Command(command_id="parity-first", submit_input=command_pb2.SubmitInput(text="first"))
            await bridge.command(thread, command)
            await model.reply(await model.request(), Text("FIRST"))
            await bridge.command(
                thread,
                command_pb2.Command(command_id="parity-stop", stop_runner_session=command_pb2.StopRunnerSession()),
            )
            folded = HistorySummaries(engine, history)
            # Both folds caught up with the whole retained history, folded again until it stops growing.
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(20), wait=wait_fixed(0.2), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    archived = await event_logs.events(thread, limit=1000)
                    assert any(entry.event.HasField("harness_exited") for entry in archived)
                    await fold_history(service_history_db_url)
                    assert await event_logs.last_cursor(thread) == archived[-1].cursor
                    history_summary = await folded.get(thread)
                    assert history_summary.folded_through == archived[-1].cursor
                    assert history_summary.feed is not None
                    assert isinstance(history_summary.feed.end, FeedEnd), f"{history_summary.feed=}"
                    assert await app.folded_through(thread) == archived[-1].cursor
        finally:
            await ingester.close()
            await directory.close()

        folded_logs = EventLogStore(engine, peer=event_logs.peer, history_reader=history, summaries=folded)
        folded_content = HistoryContentStore(engine, history_reader=history)
        history_threads = await ThreadStore(engine, folded).list_threads()
        app_threads = await ThreadStore(engine, app).list_threads()
        assert history_threads == app_threads, _thread_view_differences(history_threads, app_threads)
        assert await folded_logs.feed_state(thread) == await event_logs.feed_state(thread)
        app_scope = await content.current_scope(thread)
        folded_scope = await folded_content.current_scope(thread)
        assert app_scope is not None
        assert folded_scope is not None
        assert folded_scope.through_cursor == app_scope.through_cursor
        assert await folded_content.admitted_command(thread, command) == await content.admitted_command(thread, command)
        assert await folded_content.evidence(
            thread,
            projection_epoch=folded_scope.projection_epoch,
            entity_kind=EntityKind.COMMAND,
            entity_id="parity-first",
            after_cursor=0,
            limit=200,
        ) == await content.evidence(
            thread,
            projection_epoch=app_scope.projection_epoch,
            entity_kind=EntityKind.COMMAND,
            entity_id="parity-first",
            after_cursor=0,
            limit=200,
        )
        # A browser holding the app projection's rows sees a reset, not the other fold's rows.
        with pytest.raises(ThreadScopeChangedError):
            await folded_content.evidence(
                thread,
                projection_epoch=app_scope.projection_epoch,
                entity_kind=EntityKind.COMMAND,
                entity_id="parity-first",
                after_cursor=0,
                limit=200,
            )
        # A confirmed restart leaves the two feed states equal, whether or not the feed had ended.
        await folded_logs.resume_pending(thread)
        await event_logs.resume_pending(thread)
        assert await folded_logs.feed_state(thread) == await event_logs.feed_state(thread)


async def test_stopped_service_never_falls_back_to_reachable_runner(
    cluster: Cluster, live_index: LiveIndex, tmp_path: Path, runner: RunnerHandle, service_history_db_url: str
) -> None:
    async with authenticated_service(
        cluster, runner.port, tmp_path / "service-token", database_url=service_history_db_url
    ) as remote:
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
