"""Committed sampler evidence replays through real authenticated Go History ingress."""

import asyncio
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import delete, text

from agentplane.history_service.testing.go_service import WRITER_AUDIENCE, go_history_service, migrate_history_database
from agentplane.proto import event_log_pb2, event_pb2, runner_pb2, sandboxes_pb2
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.electric_role import ensure_electric_role
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.x.central_sampler import exporter, sampler_pb2
from agentplane.x.central_sampler.exporter import checkpoint, export_session, exporting
from agentplane.x.central_sampler.migration import runner
from agentplane.x.central_sampler.store import ExportCheckpoint, Session, Store
from util.testing.postgres import create_temporary_database
from util.testing.sqlalchemy_engines import postgres_async_engine


async def test_durable_export_reconciles_a_lost_acknowledgement(
    database_url: str, postgres_admin_url: str, api_server: KubernetesApiServer, tmp_path: Path
) -> None:
    writer = ServiceAccountRef(namespace="sampler-test", name="central-sampler")
    session_id = str(uuid4())
    request = sampler_pb2.OpenSessionRequest(
        session_id=session_id,
        spec=runner_pb2.SessionSpec(
            harness=runner_pb2.HARNESS_CENTRAL_SAMPLER,
            cwd="/workspace",
            model="model",
            central_sampler=runner_pb2.CentralSamplerConfig(service="central", provider="responses"),
        ),
        executor=sandboxes_pb2.SandboxDestination(
            owner=sandboxes_pb2.ServiceAccount(namespace="execution", name="executor"),
            sandbox="executor",
            sandbox_uid=str(uuid4()),
        ),
        expected_incarnation_uid=str(uuid4()),
    )
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as connection:
            await connection.run_sync(runner().run_for_connection)
        store = Store(engine)
        await store.open_session(request, "sampler-client-uid")
        async with store.sessions.begin() as db:
            _, session = await store.lock_session(db, session_id)
            boundary = await store.append(
                db,
                session,
                event_pb2.Event(
                    boundary_record=event_pb2.BoundaryRecord(
                        boundary="provider",
                        direction="response",
                        payload=b'{"content":"retained"}',
                        media_type="application/json",
                    )
                ),
            )
            await store.append(
                db,
                session,
                event_pb2.Event(
                    source_sequences=[boundary.cursor],
                    item_started=event_pb2.ItemStarted(item_id="answer", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT),
                ),
            )
            await store.append(
                db,
                session,
                event_pb2.Event(
                    source_sequences=[boundary.cursor],
                    item_completed=event_pb2.ItemCompleted(item_id="answer", text="Central answer"),
                ),
            )
        async with store.sessions.begin() as db:
            _, session = await store.lock_session(db, session_id)
            for sequence in range(40):
                await store.append(
                    db,
                    session,
                    event_pb2.Event(
                        boundary_record=event_pb2.BoundaryRecord(
                            boundary="provider",
                            direction="response",
                            payload=f'{{"delta":{sequence}}}'.encode(),
                            media_type="application/json",
                        )
                    ),
                )
        assert await checkpoint(store, session_id, "pending-history") is None
        ensure_electric_role(postgres_admin_url)
        with create_temporary_database(postgres_admin_url, "central_history_" + uuid4().hex) as history_url:
            migrate_history_database(history_url)
            token_file = tmp_path / "sampler-history-token"
            token_file.write_text(await api_server.workload_token(writer, [WRITER_AUDIENCE]))
            async with go_history_service(
                history_url,
                api=api_server,
                directory=tmp_path,
                writer_namespaces=[writer.namespace],
                sampler_accounts=[writer],
            ) as history:
                events = store.events
                appended = False

                async def events_with_new_commit(
                    session_id: str, after: int, *, limit: int = 256
                ) -> list[event_log_pb2.EventEntry]:
                    nonlocal appended
                    entries = await events(session_id, after, limit=limit)
                    if not appended:
                        appended = True
                        async with store.sessions.begin() as db:
                            _, current = await store.lock_session(db, session_id)
                            await store.append(
                                db,
                                current,
                                event_pb2.Event(
                                    execution_progress=event_pb2.ExecutionProgress(
                                        operation_id=session_id, kind="session", phase="blocked", payload_json="{}"
                                    )
                                ),
                            )
                            current.blocked = True
                    return entries

                # One export drains multiple pages and commits made while its original snapshot
                # is in flight. No elapsed-time threshold or timer wakeup is involved.
                with patch.object(store, "events", side_effect=events_with_new_commit):
                    await export_session(store, session, history.ingress_target, token_file)
                session = await store.get_session(session_id)
                original = await store.events(session_id, 0)
                assert await checkpoint(store, session_id, history.ingress_target) == session.cursor
                page = await history.reader.read_session_events(session_id)
                assert list(page.entries) == original
                assert page.feed_state.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
                await history.folded(UUID(session_id), session.cursor)
                # Model the crash window where History committed and our local acknowledgement
                # transaction was lost. The remote cursor reconciles without duplicate evidence.
                async with store.sessions.begin() as db:
                    await db.execute(delete(ExportCheckpoint))
                await export_session(store, session, history.ingress_target, token_file)
                again = await history.reader.read_session_events(session_id)
                assert list(again.entries) == original
                assert await checkpoint(store, session_id, history.ingress_target) == session.cursor
                summary = await history.reader.get_session_summary(session_id)
                assert summary.sandbox.name == "executor"

                idle, acknowledged = asyncio.Event(), asyncio.Event()
                pending = exporter.pending_sessions
                commit = exporter.committed
                expected_cursor = session.cursor

                async def observe_pending(
                    store: Store, target: str, *, exclude: set[str], after: str, limit: int
                ) -> list[Session]:
                    sessions = await pending(store, target, exclude=exclude, after=after, limit=limit)
                    if not sessions and not exclude:
                        idle.set()
                    return sessions

                async def observe_ack(store: Store, session_id: str, target: str, cursor: int) -> None:
                    await commit(store, session_id, target, cursor)
                    if cursor == expected_cursor:
                        acknowledged.set()

                other_replica = Store(engine)
                with (
                    patch.object(exporter, "pending_sessions", side_effect=observe_pending),
                    patch.object(exporter, "committed", side_effect=observe_ack),
                ):
                    async with exporting(store, history.ingress_target, token_file), asyncio.timeout(30):
                        await idle.wait()
                        for disconnect in (False, True):
                            idle.clear()
                            acknowledged.clear()
                            expected_cursor += 1
                            if disconnect:
                                # A commit in a LISTEN gap must be found by registration catch-up,
                                # even if it produces no notification on the replacement connection.
                                async with engine.begin() as connection:
                                    await connection.execute(
                                        text(
                                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                            "WHERE application_name = 'central-sampler-history-export'"
                                        )
                                    )
                            async with other_replica.sessions.begin() as db:
                                _, current = await other_replica.lock_session(db, session_id)
                                await other_replica.append(
                                    db,
                                    current,
                                    event_pb2.Event(
                                        boundary_record=event_pb2.BoundaryRecord(
                                            boundary="provider",
                                            direction="response",
                                            payload=b'{"delta":"another replica"}',
                                            media_type="application/json",
                                        )
                                    ),
                                )
                            await acknowledged.wait()
                            await idle.wait()
                retained = await history.reader.read_session_events(session_id)
                assert list(retained.entries) == await store.events(session_id, 0)


if __name__ == "__main__":
    pytest_bazel.main()

# Generated protobuf stubs require a direct typing dependency.
# gazelle:include_dep @pypi//protobuf

# gazelle:include_dep @pypi//grpcio
