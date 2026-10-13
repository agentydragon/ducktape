"""The `Runner` proxy on two gateway replicas over the fake cluster, with a scripted runner holding its
Sandbox's control stream to one of them."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import grpc
import pytest
import pytest_bazel

from agentplane.grpc_serving import serving
from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2, runner_control_pb2_grpc, runner_pb2
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionLeases
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.runner_proxy import SANDBOX_HEADER, Resources, RunnerProxy
from agentplane.runner_gateway.testing.runner_control import (
    OTHER_REPLICA,
    REPLICA,
    ScriptedRunner,
    connected,
    serve_replica_control,
)
from agentplane.sandbox_service.testing.kubernetes import SANDBOX, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict

# gazelle:include_dep @pypi//protobuf

AUDIENCE = "test-runner-proxy"
CALLER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-caller")
CONTROLLER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-sandbox-controller")
TOKENS = {CALLER: "test-caller-token", CONTROLLER: "test-controller-token"}
COMMAND = command_pb2.Command(command_id="test-command", submit_input=command_pb2.SubmitInput(text="test input"))


@dataclass
class Replica:
    """One gateway replica: its control stream listener, its proxy target and its runner connections."""

    listener: str
    target: str
    control: LifecycleControl


@pytest.fixture
def replica(
    cluster: Cluster, leases: ConnectionLeases
) -> Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]:
    for account, token in TOKENS.items():
        cluster.fake.tokens[token] = TokenVerdict(
            username=f"system:serviceaccount:{account.namespace}:{account.name}",
            pod_name=f"{account.name}-pod",
            pod_uid=f"{account.name}-pod-uid",
            audiences=(AUDIENCE,),
        )

    @asynccontextmanager
    async def start(holder: ConnectionHolder) -> AsyncIterator[Replica]:
        control = LifecycleControl(leases)
        server = grpc.aio.server()
        server.add_generic_rpc_handlers(
            (
                RunnerProxy(
                    Resources(
                        principals=cluster.build_principal_resolver(AUDIENCE),
                        caller_accounts=frozenset({CALLER}),
                        sandboxes=cluster.inventory,
                        control=control,
                        sandbox_controller_account=CONTROLLER,
                    )
                ),
            )
        )
        async with (
            serve_replica_control(
                cluster,
                holder,
                control=control,
                timing=ControlTiming(heartbeat_s=0.05, liveness_s=5, touch_s=0.1, max_age_s=60),
            ) as listener,
            serving(server, "127.0.0.1:0", grace_s=0) as port,
        ):
            yield Replica(listener=listener, target=f"127.0.0.1:{port}", control=control)

    return start


@pytest.fixture
async def holder(replica: Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]) -> AsyncIterator[Replica]:
    async with replica(REPLICA) as started:
        yield started


@pytest.fixture
async def other(replica: Callable[[ConnectionHolder], AbstractAsyncContextManager[Replica]]) -> AsyncIterator[Replica]:
    async with replica(OTHER_REPLICA) as started:
        yield started


@asynccontextmanager
async def runner_on(replica: Replica) -> AsyncIterator[ScriptedRunner]:
    runner = ScriptedRunner()
    async with connected(replica.listener, runner, replica.control):
        yield runner


def metadata(account: ServiceAccountRef = CALLER, *, sandbox: str | None = SANDBOX) -> tuple[tuple[str, str], ...]:
    named = () if sandbox is None else ((SANDBOX_HEADER, sandbox),)
    return (("authorization", f"Bearer {TOKENS[account]}"), *named)


@asynccontextmanager
async def runner_stub(replica: Replica) -> AsyncIterator[runner_control_pb2_grpc.RunnerAsyncStub]:
    async with grpc.aio.insecure_channel(replica.target) as channel:
        yield runner_control_pb2_grpc.RunnerStub(channel)


async def test_every_call_reaches_the_runner_with_its_request_and_response_intact(
    holder: Replica, other: Replica
) -> None:
    admitted = event_log_pb2.EventEntry(cursor=1)
    admitted.event.command_admitted.command.CopyFrom(COMMAND)
    async with runner_on(holder) as runner, runner_stub(holder) as stub:
        runner.sessions["test-session"] = runner_pb2.SessionSummary(session_id="test-session", last_cursor=7)
        listed = await stub.ListSessions(runner_pb2.ListSessionsRequest(), metadata=metadata())
        assert [(row.session_id, row.last_cursor) for row in listed.sessions] == [("test-session", 7)]

        delivering = asyncio.ensure_future(
            stub.DeliverCommand(
                runner_control_pb2.DeliverCommandRequest(session_id="test-session", command=COMMAND),
                metadata=metadata(),
            )
        )
        delivered = await runner.next(runner_control_pb2.DeliverCommandRequest)
        assert (delivered.request.session_id, delivered.request.command) == ("test-session", COMMAND)
        delivered.outcome.set_result(runner_control_pb2.DeliverCommandResponse(admission=admitted))
        assert (await delivering).admission == admitted


async def test_the_runners_refusal_is_the_callers_status(holder: Replica) -> None:
    async with runner_on(holder) as runner, runner_stub(holder) as stub:
        resuming = asyncio.ensure_future(
            stub.ResumeSession(runner_control_pb2.ResumeSessionRequest(session_id="test-session"), metadata=metadata())
        )
        (await runner.next(runner_control_pb2.ResumeSessionRequest)).outcome.set_result(grpc.StatusCode.NOT_FOUND)
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await resuming
        assert refused.value.code() == grpc.StatusCode.NOT_FOUND


async def test_a_lost_connection_fails_the_call_unavailable(holder: Replica) -> None:
    async with runner_stub(holder) as stub:
        async with runner_on(holder) as runner:
            opening = asyncio.ensure_future(
                stub.OpenSession(runner_control_pb2.OpenSessionRequest(session_id="test-session"), metadata=metadata())
            )
            await runner.next(runner_control_pb2.OpenSessionRequest)
        with pytest.raises(grpc.aio.AioRpcError) as lost:
            await opening
        assert lost.value.code() == grpc.StatusCode.UNAVAILABLE


async def test_only_the_sandbox_controller_seals(holder: Replica) -> None:
    async with runner_on(holder) as runner, runner_stub(holder) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await stub.Seal(runner_pb2.SealRequest(), metadata=metadata(CALLER))
        assert refused.value.code() == grpc.StatusCode.PERMISSION_DENIED
        assert not runner.received

        sealing = asyncio.ensure_future(stub.Seal(runner_pb2.SealRequest(), metadata=metadata(CONTROLLER)))
        sealed = await runner.next(runner_pb2.SealRequest)
        sealed.outcome.set_result(
            runner_pb2.SealResponse(sessions=[runner_pb2.SessionSeal(session_id="test-session", cursor=3)])
        )
        assert [(seal.session_id, seal.cursor) for seal in (await sealing).sessions] == [("test-session", 3)]


async def test_only_the_sandbox_controller_quiesces(holder: Replica) -> None:
    async with runner_on(holder) as runner, runner_stub(holder) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await stub.Quiesce(runner_pb2.QuiesceRequest(), metadata=metadata(CALLER))
        assert refused.value.code() == grpc.StatusCode.PERMISSION_DENIED
        assert not runner.received

        quiescing = asyncio.ensure_future(stub.Quiesce(runner_pb2.QuiesceRequest(), metadata=metadata(CONTROLLER)))
        quiesced = await runner.next(runner_pb2.QuiesceRequest)
        quiesced.outcome.set_result(
            runner_pb2.QuiesceResponse(sessions=[runner_pb2.SessionSeal(session_id="test-session", cursor=3)])
        )
        assert [(seal.session_id, seal.cursor) for seal in (await quiescing).sessions] == [("test-session", 3)]


async def test_a_replica_without_the_connection_refuses_before_acting(holder: Replica, other: Replica) -> None:
    async with runner_on(holder) as runner, runner_stub(other) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await stub.ListSessions(runner_pb2.ListSessionsRequest(), metadata=metadata())
        assert refused.value.code() == grpc.StatusCode.UNAVAILABLE
    assert not runner.received


@pytest.mark.parametrize(
    ("call_metadata", "code"),
    [
        (metadata(sandbox="test-missing-sandbox"), grpc.StatusCode.NOT_FOUND),
        (metadata(sandbox=None), grpc.StatusCode.INVALID_ARGUMENT),
        (metadata()[1:], grpc.StatusCode.UNAUTHENTICATED),
    ],
    ids=["unknown-sandbox", "no-sandbox", "no-bearer"],
)
async def test_a_call_that_names_no_servable_sandbox_is_refused(
    holder: Replica, call_metadata: tuple[tuple[str, str], ...], code: grpc.StatusCode
) -> None:
    async with runner_on(holder) as runner, runner_stub(holder) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await stub.ListSessions(runner_pb2.ListSessionsRequest(), metadata=call_metadata)
        assert refused.value.code() == code
    assert not runner.received


if __name__ == "__main__":
    pytest_bazel.main()
