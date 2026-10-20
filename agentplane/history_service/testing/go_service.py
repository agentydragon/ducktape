"""Run the built Go History Service for consumer tests.

The service runs on a database the Go migrator built. A consumer reads it through the Python
`HistoryServiceClient` and writes it as a runner does, by pushing a journal to the ingress. A real
API server answers the service's TokenReviews for real Pod-bound tokens.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


@dataclass(frozen=True)
class GoHistoryService:
    read_target: str
    ingress_target: str
    reader_token_file: Path
    api: KubernetesApiServer
    process: GoProcess

    def client(self) -> HistoryServiceClient:
        """A new client of the read API, to enter."""
        return HistoryServiceClient(self.read_target, token_file=self.reader_token_file, request_timeout_s=20)

    async def writer_token(self, writer: ServiceAccountRef) -> str:
        """A token the ingress accepts as `writer`."""
        return await self.api.workload_token(writer, [WRITER_AUDIENCE])

    async def push(
        self,
        session_id: UUID,
        entries: Sequence[event_log_pb2.EventEntry],
        *,
        writer: ServiceAccountRef = WRITER,
        attached: runner_pb2.Attached | None = None,
    ) -> None:
        """Push `entries` of the Session's journal as `writer`'s runner does, returning once they are committed.

        `attached` is the runner's snapshot, announced first; without one the announcement names only the Session.
        """
        announce = runner_pb2.Attached()
        if attached is not None:
            announce.CopyFrom(attached)
        announce.session_id = str(session_id)
        async with asyncio.timeout(_PROGRESS_BOUND_SECONDS), grpc.aio.insecure_channel(self.ingress_target) as channel:
            call = runner_ingress_pb2_grpc.RunnerIngressStub(channel).Push(
                metadata=(("authorization", f"Bearer {await self.writer_token(writer)}"),)
            )
            await call.write(runner_ingress_pb2.PushFrame(announce=announce))
            for entry in entries:
                await call.write(runner_ingress_pb2.PushFrame(entry=entry))
            # Closing the call commits what arrived without acknowledging it, so wait for the acknowledgement first.
            acknowledged = 0
            while acknowledged < max((entry.cursor for entry in entries), default=0):
                acknowledgement = await call.read()
                assert isinstance(acknowledgement, runner_ingress_pb2.Acknowledgement), (
                    f"the ingress closed the call at cursor {acknowledged}"
                )
                acknowledged = acknowledgement.cursor
            await call.done_writing()
            assert await call.code() == grpc.StatusCode.OK, await call.details()

    async def folded(self, session_id: UUID, through: int) -> history_service_pb2.SessionSummary:
        """The Session's summary once the fold has reached `through`, awaited on the fold's change notices."""
        async with asyncio.timeout(_PROGRESS_BOUND_SECONDS), self.client() as client:
            async for _ in client.watch_session_summaries():
                try:
                    summary = await client.get_session_summary(str(session_id))
                except ServiceError as error:
                    if error.code != grpc.StatusCode.NOT_FOUND:
                        raise
                    continue
                if summary.folded_through >= through:
                    return summary
        raise AssertionError(f"the summaries watch ended before the fold reached {through} of {session_id}")


@asynccontextmanager
async def go_history_service(
    database_url: str,
    *,
    api: KubernetesApiServer,
    directory: Path,
    port_factory: Callable[[], int],
    watch_lease_s: float = 300,
) -> AsyncIterator[GoHistoryService]:
    """The Go History Service on `database_url`, which the Go migrator has built, authenticating through `api`."""
    read_port, ingress_port = port_factory(), port_factory()
    token_file = directory / "history-reader-token"
    token_file.write_text(await api.workload_token(READER, [READER_AUDIENCE]))
    config = directory / "history-service.yaml"
    config.write_text(
        json.dumps(
            {
                "reader_accounts": [{"namespace": READER.namespace, "name": READER.name}],
                "token_audience": READER_AUDIENCE,
                "request_timeout_s": 10,
                "host": "127.0.0.1",
                "port": read_port,
                "kubeconfig": str(api.kubeconfig),
                "ingress": {
                    "token_audience": WRITER_AUDIENCE,
                    "writer_namespaces": [WRITER.namespace],
                    "port": ingress_port,
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
        health_port=read_port,
        grace_s=_GRACE_SECONDS,
    ) as process:
        yield GoHistoryService(
            read_target=f"127.0.0.1:{read_port}",
            ingress_target=f"127.0.0.1:{ingress_port}",
            reader_token_file=token_file,
            api=api,
            process=process,
        )
