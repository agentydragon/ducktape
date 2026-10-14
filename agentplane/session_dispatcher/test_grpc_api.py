"""The Dispatcher's gRPC surface over real gRPC and TokenReview, with delivery not running so a
command stays where the API left it."""

import asyncio
from collections.abc import AsyncIterator, Callable

import grpc
import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane import sandbox_resource
from agentplane.grpc_calls import dial
from agentplane.grpc_serving import serving
from agentplane.proto import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.sandbox_resource import Sandbox
from agentplane.sandboxes.testing.kubernetes import OWNER, SANDBOX, SANDBOX_UID, SandboxCluster
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches
from agentplane.session_dispatcher.grpc_api import Resources, add_service
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch
from agentplane.session_dispatcher.store import DispatchStore
from agentplane.session_dispatcher.testing.sandbox_status import set_sandbox
from agentplane.session_dispatcher.testing.scripted_runner import ScriptedRunner
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import TokenVerdict

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

AUDIENCE = "test-session-dispatcher"
CALLER_TOKEN = "test-only-caller-token"
OTHER_CALLER = ServiceAccountRef(namespace=OWNER.namespace, name="other-caller")
OTHER_CALLER_TOKEN = "test-only-other-caller-token"
QUEUE_CAP = 2
SESSION = "test-session"
pb = session_dispatcher_pb2
type Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub


@pytest.fixture
async def stub(
    cluster: SandboxCluster,
    engine: AsyncEngine,
    sandboxes: SandboxWatch,
    runner_client: RunnerClient,
    ended: EndedDispatches,
) -> AsyncIterator[Dispatcher]:
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="test-caller",
        pod_uid="test-caller-uid",
        audiences=(AUDIENCE,),
    )
    cluster.fake.tokens[OTHER_CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OTHER_CALLER.namespace}:{OTHER_CALLER.name}",
        pod_name="test-other-caller",
        pod_uid="test-other-caller-uid",
        audiences=(AUDIENCE,),
    )
    store = DispatchStore(engine, queue_cap=QUEUE_CAP)
    resources = Resources(
        principals=cluster.build_principal_resolver(AUDIENCE),
        caller_accounts=frozenset({OWNER, OTHER_CALLER}),
        wake_caller_accounts=frozenset({OWNER}),
        store=store,
        sandboxes=sandboxes,
        runner=runner_client,
        ended=ended,
        request_timeout_s=5,
        watch_lease_s=30,
    )
    server = grpc.aio.server()
    add_service(resources, server)
    async with (
        store.wakeups.listener.listen(),
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        dial(f"127.0.0.1:{port}") as channel,
    ):
        dispatcher: Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        yield dispatcher


async def wait_for_sandbox(sandboxes: SandboxWatch, predicate: Callable[[Sandbox], bool]) -> None:
    changed = asyncio.Event()
    with sandboxes.changed.subscribe(changed):
        while True:
            changed.clear()
            sandbox = sandboxes.get_sandbox(SANDBOX)
            if sandbox is not None and predicate(sandbox):
                return
            await changed.wait()


BEARER = (("authorization", f"Bearer {CALLER_TOKEN}"),)


async def submit(stub: Dispatcher, command_id: str, *, sandbox: str = SANDBOX) -> pb.Dispatch:
    response = await stub.SubmitInput(
        pb.SubmitInputRequest(command_id=command_id, session_id=SESSION, sandbox=sandbox, text="hello"), metadata=BEARER
    )
    return response.dispatch


async def test_a_submitted_input_is_stored_queued_and_a_repeat_returns_it(stub: Dispatcher) -> None:
    queued = await submit(stub, "a")
    assert (queued.state, queued.sandbox_uid) == (pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER, SANDBOX_UID)
    assert await submit(stub, "a") == queued
    fetched = await stub.GetDispatch(pb.GetDispatchRequest(command_id="a"), metadata=BEARER)
    assert fetched.dispatch == queued
    for scope in (pb.DispatchFilter(session_id=SESSION), pb.DispatchFilter(sandbox=SANDBOX)):
        listed = await stub.ListDispatches(pb.ListDispatchesRequest(filter=scope), metadata=BEARER)
        assert [d.command_id for d in listed.dispatches] == ["a"]


async def test_a_command_id_is_one_piece_of_work(stub: Dispatcher) -> None:
    await submit(stub, "a")
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.ChangeModel(
            pb.ChangeModelRequest(command_id="a", session_id=SESSION, sandbox=SANDBOX, model="test-model"),
            metadata=BEARER,
        )
    assert refused.value.code() is grpc.StatusCode.ALREADY_EXISTS


async def test_a_full_queue_refuses_more_and_a_stop_cancels_it(stub: Dispatcher) -> None:
    for number in range(QUEUE_CAP):
        await submit(stub, f"input-{number}")
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await submit(stub, "one-too-many")
    assert refused.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
    stopped = await stub.StopRunnerSession(
        pb.StopRunnerSessionRequest(command_id="stop", session_id=SESSION, sandbox=SANDBOX), metadata=BEARER
    )
    assert stopped.dispatch.operation == pb.OPERATION_STOP_RUNNER_SESSION
    cancelled = await stub.GetDispatch(pb.GetDispatchRequest(command_id="input-0"), metadata=BEARER)
    assert cancelled.dispatch.state == pb.DELIVERY_STATE_CANCELLED_IN_DISPATCHER


async def test_a_sender_cancels_a_queued_command_once(stub: Dispatcher) -> None:
    await submit(stub, "a")
    cancelled = await stub.CancelDispatch(pb.CancelDispatchRequest(command_id="a"), metadata=BEARER)
    assert cancelled.dispatch.state == pb.DELIVERY_STATE_CANCELLED_IN_DISPATCHER
    with pytest.raises(grpc.aio.AioRpcError) as again:
        await stub.CancelDispatch(pb.CancelDispatchRequest(command_id="a"), metadata=BEARER)
    assert again.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    with pytest.raises(grpc.aio.AioRpcError) as missing:
        await stub.CancelDispatch(pb.CancelDispatchRequest(command_id="missing"), metadata=BEARER)
    assert missing.value.code() is grpc.StatusCode.NOT_FOUND


async def test_a_command_for_a_sandbox_that_does_not_exist_is_refused_and_stores_nothing(stub: Dispatcher) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await submit(stub, "a", sandbox="no-such-sandbox")
    assert refused.value.code() is grpc.StatusCode.NOT_FOUND
    listed = await stub.ListDispatches(
        pb.ListDispatchesRequest(filter=pb.DispatchFilter(session_id=SESSION)), metadata=BEARER
    )
    assert not listed.dispatches


async def test_only_authenticated_configured_callers_can_mark_work_wake_eligible(
    stub: Dispatcher, engine: AsyncEngine
) -> None:
    # The flag is derived from TokenReview identity and server configuration, never from RPC input.
    await stub.SubmitInput(
        pb.SubmitInputRequest(command_id="app-send", session_id=SESSION, sandbox=SANDBOX, text="hello"), metadata=BEARER
    )
    await stub.SubmitInput(
        pb.SubmitInputRequest(command_id="other-send", session_id="other-session", sandbox=SANDBOX, text="hello"),
        metadata=(("authorization", f"Bearer {OTHER_CALLER_TOKEN}"),),
    )
    store = DispatchStore(engine, queue_cap=QUEUE_CAP)
    assert (await store.get_dispatch("app-send")).wake_eligible
    assert not (await store.get_dispatch("other-send")).wake_eligible


async def test_an_interrupt_for_a_sandbox_that_does_not_exist_is_refused(stub: Dispatcher) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.InterruptTurn(
            pb.InterruptTurnRequest(command_id="i", session_id=SESSION, sandbox="no-such-sandbox", turn_id="t"),
            metadata=BEARER,
        )
    assert refused.value.code() is grpc.StatusCode.NOT_FOUND


async def test_a_watch_shows_a_session_dispatches_and_each_change_to_them(stub: Dispatcher) -> None:
    await submit(stub, "a")
    watch = stub.WatchDispatches(
        pb.WatchDispatchesRequest(filter=pb.DispatchFilter(session_id=SESSION)), metadata=BEARER
    )
    events = aiter(watch)
    opened = await anext(events)
    assert (opened.dispatch.command_id, opened.dispatch.state) == ("a", pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER)
    await stub.CancelDispatch(pb.CancelDispatchRequest(command_id="a"), metadata=BEARER)
    changed = await anext(events)
    assert (changed.dispatch.command_id, changed.dispatch.state) == ("a", pb.DELIVERY_STATE_CANCELLED_IN_DISPATCHER)
    watch.cancel()


async def test_a_request_without_a_scope_or_an_id_is_invalid(stub: Dispatcher) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as unscoped:
        await stub.ListDispatches(pb.ListDispatchesRequest(), metadata=BEARER)
    assert unscoped.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.aio.AioRpcError) as anonymous:
        await submit(stub, "")
    assert anonymous.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_a_caller_without_a_known_bearer_is_unauthenticated(stub: Dispatcher) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.GetDispatch(pb.GetDispatchRequest(command_id="a"), metadata=(("authorization", "Bearer other"),))
    assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def test_a_session_opening_is_stored_and_its_id_is_the_idempotency_key(stub: Dispatcher) -> None:
    request = pb.OpenSessionRequest(session_id=SESSION, sandbox=SANDBOX)
    request.spec.model = "test-model"
    opened = (await stub.OpenSession(request, metadata=BEARER)).opening
    assert (opened.state, opened.sandbox_uid) == (pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER, SANDBOX_UID)
    assert (await stub.OpenSession(request, metadata=BEARER)).opening == opened
    assert (
        await stub.GetSessionOpening(pb.GetSessionOpeningRequest(session_id=SESSION), metadata=BEARER)
    ).opening == opened
    request.spec.model = "other-model"
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.OpenSession(request, metadata=BEARER)
    assert refused.value.code() is grpc.StatusCode.ALREADY_EXISTS


async def test_an_authenticated_app_opening_is_wake_eligible(stub: Dispatcher, engine: AsyncEngine) -> None:
    await stub.OpenSession(pb.OpenSessionRequest(session_id=SESSION, sandbox=SANDBOX), metadata=BEARER)
    store = DispatchStore(engine, queue_cap=QUEUE_CAP)
    assert (await store.get_opening(SESSION)).wake_eligible


async def test_a_session_cannot_be_opened_in_a_sandbox_that_does_not_exist(stub: Dispatcher) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await stub.OpenSession(pb.OpenSessionRequest(session_id=SESSION, sandbox="missing-sandbox"), metadata=BEARER)
    assert refused.value.code() is grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.aio.AioRpcError) as unknown:
        await stub.GetSessionOpening(pb.GetSessionOpeningRequest(session_id=SESSION), metadata=BEARER)
    assert unknown.value.code() is grpc.StatusCode.NOT_FOUND


async def test_an_interrupt_is_delivered_to_a_ready_runner_and_otherwise_nothing_happens(
    stub: Dispatcher, cluster: SandboxCluster, runner: tuple[ScriptedRunner, str], sandboxes: SandboxWatch
) -> None:
    request = pb.InterruptTurnRequest(command_id="interrupt", session_id=SESSION, sandbox=SANDBOX, turn_id="test-turn")
    set_sandbox(cluster, ready=False)
    await wait_for_sandbox(sandboxes, lambda sandbox: not sandbox.ready)
    unready = await stub.InterruptTurn(request, metadata=BEARER)
    assert (unready.state, unready.reason) == (pb.DELIVERY_STATE_NO_OP, "sandbox_not_ready")
    set_sandbox(cluster, ready=False, running=False, wake_on_send=True)
    await wait_for_sandbox(sandboxes, lambda sandbox: sandbox.suspended and sandbox.spec.wake_on_send)
    suspended = await stub.InterruptTurn(request, metadata=BEARER)
    assert (suspended.state, suspended.reason) == (pb.DELIVERY_STATE_NO_OP, "sandbox_suspended")
    assert cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["spec"]["running"] is False
    assert runner[0].calls == []
    set_sandbox(cluster, ready=True)
    await wait_for_sandbox(sandboxes, lambda sandbox: sandbox.ready)
    delivered = await stub.InterruptTurn(request, metadata=BEARER)
    assert delivered.state == pb.DELIVERY_STATE_RUNNER_ADMITTED
    assert runner[0].calls == [("InterruptTurn", "interrupt")]


if __name__ == "__main__":
    pytest_bazel.main()
