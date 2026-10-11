"""Real gRPC and TokenReview against a migrated history database."""

from collections.abc import AsyncIterator
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentplane.history_service import protocol_pb2 as history_pb2, protocol_pb2_grpc
from agentplane.history_service.evidence import EvidenceReader
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.summary_changes import SummaryChanges, notify_summaries
from agentplane.history_service.thread_reads import ThreadReader
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.sandbox_service.session_history.history_changes import HistoryChanges
from agentplane.sandbox_service.session_history.store import Store
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict, fake_apiserver
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/sandbox_service/session_history:conftest

pytest_plugins = ("agentplane.sandbox_service.session_history.conftest",)

AUDIENCE = "test-history-service"
READER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-history-reader")
READER_TOKEN = "test-history-reader-token"
OTHER_TOKEN = "test-history-other-caller-token"
WATCH_LEASE_S = 2
EVENT = event_log_pb2.EventEntry(
    cursor=1,
    origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=1),
    event=event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text="retained")),
)


@pytest.fixture
def history(engine: AsyncEngine) -> Store:
    return Store(engine)


@pytest.fixture
async def stub(engine: AsyncEngine, history: Store) -> AsyncIterator[protocol_pb2_grpc.HistoryServiceAsyncStub]:
    async with fake_apiserver() as fake:
        for token, name in ((READER_TOKEN, READER.name), (OTHER_TOKEN, "test-other-caller")):
            fake.tokens[token] = TokenVerdict(
                username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{name}",
                pod_name=f"{name}-pod",
                pod_uid=f"{name}-pod-uid",
                audiences=(AUDIENCE,),
            )
        history_changes = HistoryChanges(engine.url)
        summary_changes = SummaryChanges(engine.url)
        async with (
            k8s_client.ApiClient(k8s_client.Configuration(host=f"http://127.0.0.1:{fake.port}")) as api,
            history_changes.listener.listen(),
            summary_changes.listener.listen(),
        ):
            server = grpc.aio.server()
            add_service(
                Resources(
                    principals=WorkloadPrincipalResolver(
                        authentication=k8s_client.AuthenticationV1Api(api),
                        audience=AUDIENCE,
                        allowed_service_account_namespaces={SANDBOX_NAMESPACE},
                    ),
                    history=history,
                    summaries=SummaryReader(engine),
                    threads=ThreadReader(engine),
                    evidence=EvidenceReader(engine, history),
                    reader_accounts=frozenset({READER}),
                    request_timeout_s=10,
                    history_changes=history_changes,
                    summary_changes=summary_changes,
                    watch_lease_s=WATCH_LEASE_S,
                ),
                server,
            )
            port = server.add_insecure_port("127.0.0.1:0")
            await server.start()
            try:
                async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                    yield protocol_pb2_grpc.HistoryServiceStub(channel)
            finally:
                await server.stop(0)


def bearer(token: str) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {token}"),)


async def test_reader_reads_history_of_a_deleted_sandbox(
    stub: protocol_pb2_grpc.HistoryServiceAsyncStub, history: Store
) -> None:
    session_id = uuid4()
    await history.record_session(
        session_id,
        sandbox_namespace=SANDBOX_NAMESPACE,
        sandbox_name="test-deleted-sandbox",
        sandbox_uid=None,
        runner_session_id="test-legacy-runner",
    )
    await history.append(session_id, [EVENT])

    page = await stub.ReadSessionEvents(
        history_pb2.ReadSessionEventsRequest(session_id=str(session_id), limit=10), metadata=bearer(READER_TOKEN)
    )
    assert page.last_cursor == 1
    assert list(page.entries) == [EVENT]
    observations = await stub.ReadSessionObservations(
        history_pb2.ReadSessionObservationsRequest(session_id=str(session_id), limit=10), metadata=bearer(READER_TOKEN)
    )
    assert observations.last_cursor == 1
    assert [(row.cursor, row.kind) for row in observations.observations] == [(1, "harness_stderr")]
    summary = await stub.GetSessionSummary(
        history_pb2.GetSessionSummaryRequest(session_id=str(session_id)), metadata=bearer(READER_TOKEN)
    )
    # Not folded yet: the lag is the whole committed prefix.
    assert (summary.sandbox_name, summary.committed_cursor, summary.HasField("projection_epoch")) == (
        "test-deleted-sandbox",
        1,
        False,
    )


@pytest.mark.parametrize(
    ("metadata", "limit", "code"),
    [
        ((), 10, grpc.StatusCode.UNAUTHENTICATED),
        (bearer("test-unknown-token"), 10, grpc.StatusCode.UNAUTHENTICATED),
        (bearer(OTHER_TOKEN), 10, grpc.StatusCode.PERMISSION_DENIED),
        (bearer(READER_TOKEN), 1001, grpc.StatusCode.INVALID_ARGUMENT),
        (bearer(READER_TOKEN), 10, grpc.StatusCode.NOT_FOUND),
    ],
)
async def test_read_refusals(
    stub: protocol_pb2_grpc.HistoryServiceAsyncStub,
    metadata: tuple[tuple[str, str], ...],
    limit: int,
    code: grpc.StatusCode,
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.ReadSessionEvents(
            history_pb2.ReadSessionEventsRequest(session_id=str(uuid4()), limit=limit), metadata=metadata
        )
    assert refused.value.code() == code


@pytest.mark.parametrize(
    ("metadata", "listing", "code"),
    [
        (bearer(OTHER_TOKEN), history_pb2.ListSessionSummariesRequest(limit=10), grpc.StatusCode.PERMISSION_DENIED),
        (bearer(READER_TOKEN), history_pb2.ListSessionSummariesRequest(limit=0), grpc.StatusCode.INVALID_ARGUMENT),
        (
            bearer(READER_TOKEN),
            history_pb2.ListSessionSummariesRequest(limit=10, page_token=b"\xff"),
            grpc.StatusCode.INVALID_ARGUMENT,
        ),
    ],
)
async def test_summary_list_refusals(
    stub: protocol_pb2_grpc.HistoryServiceAsyncStub,
    metadata: tuple[tuple[str, str], ...],
    listing: history_pb2.ListSessionSummariesRequest,
    code: grpc.StatusCode,
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.ListSessionSummaries(listing, metadata=metadata)
    assert refused.value.code() == code


async def test_thread_read_refusals(stub: protocol_pb2_grpc.HistoryServiceAsyncStub) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as no_window:
        await stub.ReadThreadEntities(
            history_pb2.ReadThreadEntitiesRequest(session_id=str(uuid4())), metadata=bearer(READER_TOKEN)
        )
    assert no_window.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_summary_watch_announces_each_fold_commit_until_planned_renewal(
    stub: protocol_pb2_grpc.HistoryServiceAsyncStub, engine: AsyncEngine
) -> None:
    call = stub.WatchSessionSummaries(history_pb2.WatchSessionSummariesRequest(), metadata=bearer(READER_TOKEN))
    opening = await call.read()
    assert isinstance(opening, history_pb2.WatchSessionSummariesResponse)
    assert opening.HasField("changed")  # whatever changed before the caller watched
    async with engine.begin() as connection, AsyncSession(bind=connection) as session:
        await notify_summaries(session)
    committed = await call.read()
    assert isinstance(committed, history_pb2.WatchSessionSummariesResponse)
    assert committed.HasField("changed")
    renewal = await call.read()
    assert isinstance(renewal, history_pb2.WatchSessionSummariesResponse)
    assert renewal.HasField("reconnect_required")
    assert await call.read() is grpc.aio.EOF


async def test_summary_watch_requires_a_reader(stub: protocol_pb2_grpc.HistoryServiceAsyncStub) -> None:
    call = stub.WatchSessionSummaries(history_pb2.WatchSessionSummariesRequest(), metadata=bearer(OTHER_TOKEN))
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await call.read()
    assert raised.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_history_watch_announces_commits_until_planned_renewal(
    stub: protocol_pb2_grpc.HistoryServiceAsyncStub, history: Store
) -> None:
    session_id = uuid4()
    await history.record_session(
        session_id,
        sandbox_namespace=SANDBOX_NAMESPACE,
        sandbox_name="test-history-watch",
        sandbox_uid=None,
        runner_session_id="test-history-watch-runner",
    )
    call = stub.WatchSessionHistory(history_pb2.WatchSessionHistoryRequest(), metadata=bearer(READER_TOKEN))
    opening = await call.read()
    assert isinstance(opening, history_pb2.WatchSessionHistoryResponse)
    assert opening.HasField("changed")
    await history.append(session_id, [EVENT])
    committed = await call.read()
    assert isinstance(committed, history_pb2.WatchSessionHistoryResponse)
    assert committed.HasField("changed")
    renewal = await call.read()
    assert isinstance(renewal, history_pb2.WatchSessionHistoryResponse)
    assert renewal.HasField("reconnect_required")
    assert await call.read() is grpc.aio.EOF


async def test_history_watch_requires_a_reader(stub: protocol_pb2_grpc.HistoryServiceAsyncStub) -> None:
    call = stub.WatchSessionHistory(history_pb2.WatchSessionHistoryRequest(), metadata=bearer(OTHER_TOKEN))
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await call.read()
    assert refused.value.code() == grpc.StatusCode.PERMISSION_DENIED


if __name__ == "__main__":
    pytest_bazel.main()
