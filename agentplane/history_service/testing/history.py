"""Scripted Session history in the Go History Service's database.

A consumer under test reads it only through the History Service, as in production. Tests write a
Session's entries and feed state here as a runner pushes them to the ingress, and each write
returns once the service has folded it. `sessions` reads the fold's rows for assertions.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from kubernetes_asyncio import client as k8s_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from agentplane import sandbox_resource
from agentplane.history_service import history_service_pb2
from agentplane.history_service.channels import Channel
from agentplane.history_service.fold.models import ThreadCheckpoint, ThreadEntity
from agentplane.history_service.fold.views import EntityKind
from agentplane.history_service.testing.go_service import WRITER, GoHistoryService, go_history_service
from agentplane.postgres.wakeups import notify
from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//protobuf


class History:
    """One test's scripted history in the database `service` runs on."""

    def __init__(self, service: GoHistoryService) -> None:
        self.service = service
        self.engine: AsyncEngine = create_async_engine(service.database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        # The runner's latest snapshot per Session, announced again with every push: an announcement replaces the
        # stored feed state, and the service stores it only once the committed entries cover its cursor.
        self._attached: dict[UUID, runner_pb2.Attached] = {}

    async def open(
        self, session_id: UUID, *, sandbox_name: str | None = None, sandbox_namespace: str | None = None
    ) -> None:
        """Bind the Session to the test writer unless a writer already pushed it."""
        if await self._writer(session_id) is not None:
            return
        writer = WRITER if sandbox_name is None else ServiceAccountRef(namespace=WRITER.namespace, name=sandbox_name)
        if sandbox_name is not None and sandbox_namespace is not None:
            writer = await self._associate_writer_with_sandbox(writer, sandbox_name, sandbox_namespace)
        await self.service.push(session_id, [], writer=writer)

    async def _associate_writer_with_sandbox(
        self, writer: ServiceAccountRef, sandbox_name: str, sandbox_namespace: str
    ) -> ServiceAccountRef:
        """Give scripted writers the live Sandbox identity when a fixture Sandbox exists."""
        custom = k8s_client.CustomObjectsApi(self.service.api.api)
        try:
            sandbox = await custom.get_namespaced_custom_object(
                sandbox_resource.GROUP,
                sandbox_resource.VERSION,
                sandbox_namespace,
                sandbox_resource.SANDBOXES_PLURAL,
                sandbox_name,
            )
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise
            return writer
        writer = ServiceAccountRef(namespace=sandbox_namespace, name=sandbox_name)
        await self.service.writer_token(writer, pod_name=f"history-writer-{uuid4().hex[:12]}")
        core = k8s_client.CoreV1Api(self.service.api.api)
        account = await core.read_namespaced_service_account(writer.name, writer.namespace)
        account.metadata.owner_references = [
            k8s_client.V1OwnerReference(
                api_version=sandbox_resource.SANDBOX_API_VERSION,
                kind=sandbox_resource.SANDBOX_KIND,
                name=sandbox_name,
                uid=sandbox["metadata"]["uid"],
                controller=True,
            )
        ]
        await core.replace_namespaced_service_account(writer.name, writer.namespace, account)
        return writer

    async def record(
        self,
        session_id: UUID,
        entries: Sequence[event_log_pb2.EventEntry],
        *,
        attached: runner_pb2.Attached | None = None,
    ) -> None:
        """Push `entries` as the Session's writer and return once the fold has caught up with them.

        `attached` is the runner's snapshot with them, which replaces the Session's last one.
        """
        if attached is not None:
            self._attached[session_id] = attached = self._snapshot(session_id, attached)
        else:
            attached = self._attached.get(session_id)
        committed = await self.service.push(
            session_id, entries, writer=await self._bound_writer(session_id), attached=attached
        )
        await self._folded(session_id, committed, attached)

    async def record_unmodeled(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> None:
        """Push `entries` the fold cannot model and return once it has reported that it stalled on them."""
        await self.service.push(
            session_id, entries, writer=await self._bound_writer(session_id), attached=self._attached.get(session_id)
        )
        await self.service.until(lambda: self._operational_status_is_failed(session_id))

    async def set_attached(self, session_id: UUID, attached: runner_pb2.Attached) -> None:
        """Announce the runner's snapshot. A snapshot ahead of the Session's entries is stored once they cover it."""
        snapshot = self._snapshot(session_id, attached)
        self._attached[session_id] = snapshot
        committed = await self.service.push(
            session_id, [], writer=await self._bound_writer(session_id), attached=snapshot
        )
        await self._folded(session_id, committed, snapshot)

    @staticmethod
    def _snapshot(session_id: UUID, attached: runner_pb2.Attached) -> runner_pb2.Attached:
        snapshot = runner_pb2.Attached()
        snapshot.CopyFrom(attached)
        snapshot.session_id = str(session_id)
        return snapshot

    async def clear_sandbox_identity(self, session_id: UUID) -> None:
        """Inject a committed History row without Sandbox identity for reader error tests."""
        async with self.engine.begin() as connection:
            result = await connection.execute(
                text("UPDATE session_history SET sandbox_namespace = NULL, sandbox_name = NULL WHERE id = :id"),
                {"id": session_id},
            )
        if result.rowcount != 1:
            raise ValueError("open the Session first")

    async def fail_feed(self, session_id: UUID, *, error: str, error_cursor: int | None = None) -> None:
        """A folded failure state for UI tests, not a fake archive failure."""
        async with self.sessions.begin() as session:
            checkpoint = await session.get(ThreadCheckpoint, session_id)
            if checkpoint is None:
                raise ValueError("record the Session's first entries before failing its feed")
            view = await session.get(
                ThreadEntity, (session_id, checkpoint.projection_epoch, EntityKind.VIEW_STATE, "current")
            )
            if view is None:
                raise ValueError("thread checkpoint has no current controls")
            view.state = {
                **view.state,
                "operational": {
                    "status": "failed",
                    "last_verified_cursor": str(checkpoint.through_cursor),
                    "feed_error": {"cursor": None if error_cursor is None else str(error_cursor), "message": error},
                },
            }
            await notify(session, Channel.SUMMARIES)

    @staticmethod
    async def fence_fold(session: AsyncSession, session_id: UUID) -> None:
        """Hold the Session's projection lease row until `session`'s transaction ends, as the fold's writes do.

        The fold runs a cycle every 50 ms and, under that lock, rewrites the Session's operational state and
        summary from the summary it read. A test that writes those rows takes the lock before reading them, so
        a cycle that read before the test's commit cannot write over it afterwards.
        """
        await session.execute(
            text("SELECT 1 FROM session_projection_lease WHERE session_id = :id FOR UPDATE"), {"id": session_id}
        )

    @asynccontextmanager
    async def fence_ingress(self, session_id: UUID) -> AsyncIterator[None]:
        """Hold the History row the runner ingress updates, until the caller leaves the context."""
        async with self.engine.begin() as connection:
            row = await connection.execute(
                text("SELECT 1 FROM session_history WHERE id = :id FOR UPDATE"), {"id": session_id}
            )
            if row.first() is None:
                raise ValueError(f"open Session {session_id} before fencing its ingress")
            yield

    async def announce_fold_commit(self) -> None:
        """Commit the notice a fold commit sends, without folding anything."""
        async with self.sessions.begin() as session:
            await notify(session, Channel.SUMMARIES)

    async def _writer(self, session_id: UUID) -> ServiceAccountRef | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT writer_namespace, writer_name FROM session_history WHERE id = :id"), {"id": session_id}
                )
            ).first()
        return None if row is None else ServiceAccountRef(namespace=row.writer_namespace, name=row.writer_name)

    async def _bound_writer(self, session_id: UUID) -> ServiceAccountRef:
        writer = await self._writer(session_id)
        if writer is None:
            raise ValueError("open the Session first")
        return writer

    async def _operational_status_is_failed(self, session_id: UUID) -> bool:
        async with self.sessions() as session:
            checkpoint = await session.get(ThreadCheckpoint, session_id)
            if checkpoint is None:
                return False
            view = await session.get(
                ThreadEntity, (session_id, checkpoint.projection_epoch, EntityKind.VIEW_STATE, "current")
            )
        if view is None:
            return False
        operational = view.state["operational"]
        return isinstance(operational, dict) and operational["status"] == "failed"

    async def _folded(self, session_id: UUID, committed: int, attached: runner_pb2.Attached | None) -> None:
        """Wait for the summary to show the entries through `committed` and, once they cover it, `attached`."""

        def shows(summary: history_service_pb2.SessionSummary) -> bool:
            if summary.folded_through < committed:
                return False
            if attached is None or attached.last_cursor > committed:
                return True
            # The summary's snapshot is the runner's with the entries after it applied.
            if attached.last_cursor == committed:
                return summary.attached == attached
            return summary.attached.last_cursor == committed

        await self.service.settled(session_id, shows)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self.engine.dispose()


@asynccontextmanager
async def scripted_history(database_url: str, *, api: KubernetesApiServer, directory: Path) -> AsyncIterator[History]:
    """The Go History Service on `database_url`, which the Go migrator has built, and a `History` scripting it."""
    async with go_history_service(database_url, api=api, directory=directory) as service, History(service) as history:
        yield history
