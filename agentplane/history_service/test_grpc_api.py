"""Real gRPC and TokenReview against a migrated history database."""

from collections.abc import AsyncIterator
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentplane.grpc_serving import serving
from agentplane.history_service.channels import Channel, create_wakeups
from agentplane.history_service.evidence import EvidenceReader
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.storage.store import Store, Writer
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.testing.sessions import WRITER, retained_session
from agentplane.history_service.thread_reads import ThreadReader
from agentplane.postgres.wakeups import notify
from agentplane.proto import event_log_pb2, event_pb2, history_service_pb2, history_service_pb2_grpc
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict, fake_apiserver
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf

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
async def stub(engine: AsyncEngine, history: Store) -> AsyncIterator[history_service_pb2_grpc.HistoryServiceAsyncStub]:
    async with fake_apiserver() as fake:
        for token, name in ((READER_TOKEN, READER.name), (OTHER_TOKEN, "test-other-caller")):
            fake.tokens[token] = TokenVerdict(
                username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{name}",
                pod_name=f"{name}-pod",
                pod_uid=f"{name}-pod-uid",
                audiences=(AUDIENCE,),
            )
        wakeups = create_wakeups(engine.url)
        async with (
            k8s_client.ApiClient(k8s_client.Configuration(host=f"http://127.0.0.1:{fake.port}")) as api,
            wakeups.listener.listen(),
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
                    caller_accounts=frozenset({READER}),
                    request_timeout_s=10,
                    summary_changes=wakeups.changes[Channel.SUMMARIES],
                    session_openings=wakeups.changes[Channel.SESSIONS],
                    watch_lease_s=WATCH_LEASE_S,
                ),
                server,
            )
            async with (
                serving(server, "127.0.0.1:0", grace_s=0) as port,
                grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel,
            ):
                yield history_service_pb2_grpc.HistoryServiceStub(channel)


def bearer(token: str) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {token}"),)


async def test_reader_reads_unfolded_history(
    stub: history_service_pb2_grpc.HistoryServiceAsyncStub, history: Store
) -> None:
    session_id = await retained_session(history, [EVENT])

    page = await stub.ReadSessionEvents(
        history_service_pb2.ReadSessionEventsRequest(session_id=str(session_id), limit=10),
        metadata=bearer(READER_TOKEN),
    )
    assert page.last_cursor == 1
    assert list(page.entries) == [EVENT]
    observations = await stub.ReadSessionObservations(
        history_service_pb2.ReadSessionObservationsRequest(session_id=str(session_id), limit=10),
        metadata=bearer(READER_TOKEN),
    )
    assert observations.last_cursor == 1
    assert [(row.cursor, row.kind) for row in observations.observations] == [(1, "harness_stderr")]
    summary = await stub.GetSessionSummary(
        history_service_pb2.GetSessionSummaryRequest(session_id=str(session_id)), metadata=bearer(READER_TOKEN)
    )
    # Not folded yet: the lag is the whole committed prefix.
    assert (summary.committed_cursor, summary.HasField("projection_epoch")) == (1, False)
    assert (summary.sandbox.namespace, summary.sandbox.name) == (WRITER.namespace, WRITER.name)


async def test_find_sessions_by_the_name_of_the_sandbox_that_wrote_them(
    stub: history_service_pb2_grpc.HistoryServiceAsyncStub, history: Store
) -> None:
    session_id = await retained_session(history, [EVENT])
    await history.bind_writer(uuid4(), Writer(namespace=WRITER.namespace, name="test-other-sandbox", uid="other-uid"))

    exact = await stub.FindSessions(
        history_service_pb2.FindSessionsRequest(sandbox_name=WRITER.name), metadata=bearer(READER_TOKEN)
    )
    partial = await stub.FindSessions(
        history_service_pb2.FindSessionsRequest(sandbox_name_contains=WRITER.name.upper()[5:]),
        metadata=bearer(READER_TOKEN),
    )

    expected = history_service_pb2.SessionSandbox(
        session_id=str(session_id), sandbox=history_service_pb2.SandboxRef(namespace=WRITER.namespace, name=WRITER.name)
    )
    assert list(exact.sessions) == list(partial.sessions) == [expected]


async def test_find_sessions_requires_a_name_match(stub: history_service_pb2_grpc.HistoryServiceAsyncStub) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.FindSessions(history_service_pb2.FindSessionsRequest(), metadata=bearer(READER_TOKEN))
    assert refused.value.code() == grpc.StatusCode.INVALID_ARGUMENT


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
    stub: history_service_pb2_grpc.HistoryServiceAsyncStub,
    metadata: tuple[tuple[str, str], ...],
    limit: int,
    code: grpc.StatusCode,
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.ReadSessionEvents(
            history_service_pb2.ReadSessionEventsRequest(session_id=str(uuid4()), limit=limit), metadata=metadata
        )
    assert refused.value.code() == code


async def test_thread_read_refusals(stub: history_service_pb2_grpc.HistoryServiceAsyncStub) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as no_window:
        await stub.ReadThreadEntities(
            history_service_pb2.ReadThreadEntitiesRequest(session_id=str(uuid4())), metadata=bearer(READER_TOKEN)
        )
    assert no_window.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_summary_watch_announces_each_fold_commit_until_planned_renewal(
    stub: history_service_pb2_grpc.HistoryServiceAsyncStub, engine: AsyncEngine
) -> None:
    call = stub.WatchSessionSummaries(history_service_pb2.WatchSessionSummariesRequest(), metadata=bearer(READER_TOKEN))
    opening = await call.read()
    assert isinstance(opening, history_service_pb2.WatchSessionSummariesResponse)
    assert opening.HasField("changed")  # whatever changed before the caller watched
    async with engine.begin() as connection, AsyncSession(bind=connection) as session:
        await notify(session, Channel.SUMMARIES)
    committed = await call.read()
    assert isinstance(committed, history_service_pb2.WatchSessionSummariesResponse)
    assert committed.HasField("changed")
    renewal = await call.read()
    assert isinstance(renewal, history_service_pb2.WatchSessionSummariesResponse)
    assert renewal.HasField("reconnect_required")
    assert await call.read() is grpc.aio.EOF


async def _next_opened(
    call: grpc.aio.UnaryStreamCall[
        history_service_pb2.WatchSessionSummariesRequest, history_service_pb2.WatchSessionSummariesResponse
    ],
) -> history_service_pb2.SessionOpened:
    """The stream's next `session_opened`, past the `changed` notices before it."""
    while True:
        message = await call.read()
        assert isinstance(message, history_service_pb2.WatchSessionSummariesResponse)
        if message.HasField("session_opened"):
            return message.session_opened
        assert message.HasField("changed")


async def test_watch_names_each_session_recorded_after_the_requested_position(
    stub: history_service_pb2_grpc.HistoryServiceAsyncStub, history: Store
) -> None:
    earlier, later, live = uuid4(), uuid4(), uuid4()
    await history.bind_writer(earlier, WRITER)
    await history.bind_writer(later, WRITER)
    call = stub.WatchSessionSummaries(
        history_service_pb2.WatchSessionSummariesRequest(opened_after=0), metadata=bearer(READER_TOKEN)
    )
    # The backlog first, in recording order, then each Session as its first push binds it.
    backlog = [await _next_opened(call), await _next_opened(call)]
    assert [opened.session_id for opened in backlog] == [str(earlier), str(later)]
    await history.bind_writer(live, WRITER)
    assert (await _next_opened(call)).session_id == str(live)
    call.cancel()

    # A caller reconnecting after the last Session it handled is offered only those recorded since.
    resumed = stub.WatchSessionSummaries(
        history_service_pb2.WatchSessionSummariesRequest(opened_after=backlog[1].position),
        metadata=bearer(READER_TOKEN),
    )
    assert (await _next_opened(resumed)).session_id == str(live)
    resumed.cancel()


async def test_summary_watch_names_no_session_unless_asked(
    stub: history_service_pb2_grpc.HistoryServiceAsyncStub, history: Store
) -> None:
    await history.bind_writer(uuid4(), WRITER)
    call = stub.WatchSessionSummaries(history_service_pb2.WatchSessionSummariesRequest(), metadata=bearer(READER_TOKEN))
    await history.bind_writer(uuid4(), WRITER)
    kinds: set[str | None] = set()
    while (message := await call.read()) is not grpc.aio.EOF:
        assert isinstance(message, history_service_pb2.WatchSessionSummariesResponse)
        kinds.add(message.WhichOneof("observation"))
    assert kinds == {"changed", "reconnect_required"}


async def test_summary_watch_requires_a_reader(stub: history_service_pb2_grpc.HistoryServiceAsyncStub) -> None:
    call = stub.WatchSessionSummaries(history_service_pb2.WatchSessionSummariesRequest(), metadata=bearer(OTHER_TOKEN))
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await call.read()
    assert raised.value.code() == grpc.StatusCode.PERMISSION_DENIED


if __name__ == "__main__":
    pytest_bazel.main()
