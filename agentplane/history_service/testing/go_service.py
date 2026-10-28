"""Run the built Go History Service for consumer tests.

The service runs on a database the Go migrator built. A consumer reads it through the Python
`HistoryServiceClient` and writes it as a runner does, by pushing a journal to the ingress. A real
API server answers the service's TokenReviews for real Pod-bound tokens.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import ExitStack, aclosing, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import grpc
from sqlalchemy.engine import make_url

from agentplane.grpc.calls import ServiceError
from agentplane.history_service import history_service_pb2, runner_ingress_pb2, runner_ingress_pb2_grpc
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.go_process import GoProcess, go_process
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.net import listen_tcp
from util.testing.postgres import PostgresDatabaseTemplate

# gazelle:include_dep @pypi//protobuf

_SERVICE = get_required_path(own_repo_rlocation("agentplane/history_service/history-service_/history-service"))
_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/history_service/migrations/cmd/history-migrate_/history-migrate")
)
_DATABASE_URL_ENV = "AGENTPLANE_HISTORY_SERVICE_DATABASE_URL"
_CONFIG_FILE_ENV = "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"
_GRACE_SECONDS = 10
# Bounds only a push or fold that stopped making progress.
_PROGRESS_BOUND_SECONDS = 30
# The ingress stores an announced snapshot only once history covers it, so a push meant to leave the feed state alone
# announces one beyond any history a test writes.
_UNCOVERED_CURSOR = 2**62

READER_AUDIENCE = "test-app-history-service"
READER = ServiceAccountRef(namespace="test-history-namespace", name="test-history-reader")
WRITER_AUDIENCE = "test-runner-ingress"
WRITER = ServiceAccountRef(namespace="test-sandbox-namespace", name="test-sandbox-runner")


def _go_database_url(database_url: str) -> str:
    """`database_url` as the Go services' PostgreSQL driver reads it."""
    return (
        make_url(database_url)
        .set(drivername="postgresql", query={"sslmode": "disable"})
        .render_as_string(hide_password=False)
    )


def migrate_history_database(database_url: str) -> None:
    """Bring a database to the History Service's schema with the Go migrator, as its init container does."""
    result = subprocess.run(
        [_MIGRATOR],
        env={_DATABASE_URL_ENV: _go_database_url(database_url)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def migrated_history_template(admin_url: str, template_name: str) -> Iterator[PostgresDatabaseTemplate]:
    """A History database template the Go migrator built once, for tests that clone many History databases."""
    with PostgresDatabaseTemplate(admin_url, template_name) as template:
        migrate_history_database(template.url)
        yield template


@dataclass(frozen=True)
class GoHistoryService:
    database_url: str
    read_target: str
    ingress_target: str
    reader_token_file: Path
    # Entered for the life of the service; `reader.new_client()` is an independent one.
    reader: HistoryServiceClient
    api: KubernetesApiServer
    process: GoProcess
    _writer_tokens: dict[ServiceAccountRef, str] = field(default_factory=dict, init=False)

    async def writer_token(self, writer: ServiceAccountRef, *, pod_name: str | None = None) -> str:
        """A token the ingress accepts as `writer`, issued once per writer: a push waits on nothing else of the API."""
        if writer not in self._writer_tokens:
            self._writer_tokens[writer] = await self.api.workload_token(writer, [WRITER_AUDIENCE], pod_name=pod_name)
        return self._writer_tokens[writer]

    async def push(
        self,
        session_id: UUID,
        entries: Sequence[event_log_pb2.EventEntry],
        *,
        writer: ServiceAccountRef = WRITER,
        attached: runner_pb2.Attached | None = None,
    ) -> int:
        """Push `entries` of the Session's journal as `writer`'s runner does, returning the cursor committed through.

        `attached` is the runner's snapshot, announced first; without one the push stores no snapshot and leaves the
        Session's feed state as it was. Without `entries` the call only binds the Session to `writer` and announces
        `attached`.
        """
        announce = runner_pb2.Attached(last_cursor=_UNCOVERED_CURSOR)
        if attached is not None:
            announce.CopyFrom(attached)
        announce.session_id = str(session_id)
        async with asyncio.timeout(_PROGRESS_BOUND_SECONDS), grpc.aio.insecure_channel(self.ingress_target) as channel:
            call = runner_ingress_pb2_grpc.RunnerIngressStub(channel).Push(
                metadata=(("authorization", f"Bearer {await self.writer_token(writer)}"),)
            )

            async def acknowledged_cursor() -> int:
                acknowledgement = await call.read()
                assert isinstance(acknowledgement, runner_ingress_pb2.Acknowledgement), (
                    "the ingress closed the call before acknowledging"
                )
                return acknowledgement.cursor

            await call.write(runner_ingress_pb2.PushFrame(announce=announce))
            for entry in entries:
                await call.write(runner_ingress_pb2.PushFrame(entry=entry))
            # Closing the call commits what arrived without acknowledging it, so wait for the acknowledgement first.
            committed = await acknowledged_cursor()
            while committed < max((entry.cursor for entry in entries), default=0):
                committed = await acknowledged_cursor()
            await call.done_writing()
            assert await call.code() == grpc.StatusCode.OK, await call.details()
            return committed

    async def until(self, holds: Callable[[], Awaitable[bool]]) -> None:
        """Return once `holds()` is true, asking at the watch's opening and after every change the fold notices."""
        async with asyncio.timeout(_PROGRESS_BOUND_SECONDS), aclosing(self.reader.watch_session_summaries()) as changes:
            async for _ in changes:
                if await holds():
                    return
        raise AssertionError("the summaries watch ended before the condition held")

    async def settled(
        self, session_id: UUID, condition: Callable[[history_service_pb2.SessionSummary], bool]
    ) -> history_service_pb2.SessionSummary:
        """The Session's summary once `condition` holds of it."""
        latest: history_service_pb2.SessionSummary | None = None

        async def holds() -> bool:
            nonlocal latest
            try:
                latest = await self.reader.get_session_summary(str(session_id))
            except ServiceError as error:
                if error.code != grpc.StatusCode.NOT_FOUND:
                    raise
                return False
            return condition(latest)

        try:
            await self.until(holds)
        except TimeoutError as error:
            error.add_note(f"the last summary of {session_id}:\n{latest}")
            raise
        assert latest is not None
        return latest

    async def folded(self, session_id: UUID, through: int) -> history_service_pb2.SessionSummary:
        """The Session's summary once the fold has reached `through`."""
        return await self.settled(session_id, lambda summary: summary.folded_through >= through)


async def wait_for_history_session(history: HistoryServiceClient, session_id: str) -> None:
    """Wait for the raw ingress opening that binds a Session's History row."""
    while True:
        async with aclosing(history.watch_session_openings(after_position=0)) as openings:
            async for opened in openings:
                if str(opened.session_id) == session_id:
                    return


@asynccontextmanager
async def go_history_service(
    database_url: str,
    *,
    api: KubernetesApiServer,
    directory: Path,
    writer_namespaces: Sequence[str] = (WRITER.namespace,),
    watch_lease_s: float = 300,
) -> AsyncIterator[GoHistoryService]:
    """The Go History Service on `database_url`, which the Go migrator has built, authenticating through `api`."""
    with ExitStack() as listeners:
        reader_listener = listeners.enter_context(listen_tcp())
        ingress_listener = listeners.enter_context(listen_tcp())
        token_file = directory / "history-reader-token"
        token_file.write_text(await api.workload_token(READER, [READER_AUDIENCE]))
        config = directory / "history-service.yaml"
        reader_fd = reader_listener.socket.fileno()
        ingress_fd = ingress_listener.socket.fileno()
        config.write_text(
            json.dumps(
                {
                    "reader_accounts": [{"namespace": READER.namespace, "name": READER.name}],
                    "token_audience": READER_AUDIENCE,
                    "request_timeout_s": 10,
                    "listen_address": f"fd:{reader_fd}",
                    "kubeconfig": str(api.kubeconfig),
                    "ingress": {
                        "token_audience": WRITER_AUDIENCE,
                        "writer_namespaces": list(writer_namespaces),
                        "listen_address": f"fd:{ingress_fd}",
                        # Tests compare the archive with the runner's journal entry by entry.
                        "settle_deltas": False,
                    },
                    # The fold also runs when a commit announces itself; the interval only bounds a missed notice.
                    "fold_interval_s": 0.05,
                    "watch_lease_s": watch_lease_s,
                }
            )
        )
        async with go_process(
            _SERVICE,
            env={_DATABASE_URL_ENV: _go_database_url(database_url), _CONFIG_FILE_ENV: str(config)},
            stderr=directory / "history-service.stderr",
            health_port=reader_listener.port,
            grace_s=_GRACE_SECONDS,
            pass_fds=(reader_fd, ingress_fd),
        ) as process:
            listeners.close()
            read_target = f"{reader_listener.host}:{reader_listener.port}"
            service = GoHistoryService(
                database_url=database_url,
                read_target=read_target,
                ingress_target=f"{ingress_listener.host}:{ingress_listener.port}",
                reader_token_file=token_file,
                reader=HistoryServiceClient(read_target, token_file=token_file, request_timeout_s=20),
                api=api,
                process=process,
            )
            async with service.reader:
                yield service
