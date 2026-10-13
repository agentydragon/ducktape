"""One replica's runner control listener over the fake cluster, trusting the Sandbox Pod's runner, and a
scripted runner that dials it."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import override
from uuid import UUID

import grpc
from google.protobuf.message import Message
from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.grpc_tunnel import local_server
from agentplane.proto import runner_control_pb2, runner_control_pb2_grpc, runner_pb2
from agentplane.runner.control import RunnerControlClient
from agentplane.runner_gateway.connection_records import ConnectionHolder, ConnectionRecords
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_control import ControlTiming
from agentplane.runner_gateway.testing.control_listener import RUNNER_TIMING, serve_runner_control
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, SANDBOX_UID, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict

# The control servicer stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_control_pb2
# gazelle:include_dep @pypi//protobuf

RUNNER_AUDIENCE = "test-runner-control"
RUNNER_TOKEN = "test-runner-token"
REPLICA = ConnectionHolder(pod_name="test-replica", pod_ip="192.0.2.1", port=18080)
OTHER_REPLICA = ConnectionHolder(pod_name="test-other-replica", pod_ip="192.0.2.2", port=18080)
HELLO = runner_control_pb2.RunnerFrame(hello=runner_control_pb2.Hello(runner_boot_id="test-boot"))
LAUNCHER_POD = f"virt-launcher-{SANDBOX}-test"
LAUNCHER_POD_UID = "test-launcher-pod-uid"


def run_in_a_vm(cluster: Cluster) -> None:
    """`SANDBOX` as a VM Sandbox: its incarnation is its VMI's virt-launcher Pod, labelled with the
    Sandbox and running as its ServiceAccount, and `RUNNER_TOKEN` is that Pod's runner's."""
    del cluster.fake.pods[SANDBOX]
    cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]["status"] = {
        "incarnation": {"name": LAUNCHER_POD, "uid": LAUNCHER_POD_UID}
    }
    cluster.fake.pods[LAUNCHER_POD] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": LAUNCHER_POD,
            "namespace": SANDBOX_NAMESPACE,
            "uid": LAUNCHER_POD_UID,
            "labels": {sandbox_resource.SANDBOX_LABEL: SANDBOX},
            "ownerReferences": [
                {
                    "apiVersion": sandbox_resource.VMI_API_VERSION,
                    "kind": sandbox_resource.VMI_KIND,
                    "name": SANDBOX,
                    "uid": "test-vmi-uid",
                    "controller": True,
                }
            ],
        },
        "spec": {"serviceAccountName": ACCOUNT, "containers": [{"name": "compute", "image": "registry.test/unused"}]},
        "status": {"phase": "Running", "podIP": "127.0.0.1", "conditions": [{"type": "Ready", "status": "True"}]},
    }
    cluster.fake.tokens[RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=LAUNCHER_POD,
        pod_uid=LAUNCHER_POD_UID,
        audiences=(RUNNER_AUDIENCE,),
    )


def attached(session_id: str, harness_state: runner_pb2.HarnessState) -> runner_pb2.Attached:
    """A runner's answer to an open or resume of a Session without setup."""
    return runner_pb2.Attached(
        session_id=session_id, harness_state=harness_state, setup_state=runner_pb2.SETUP_STATE_NOT_REQUIRED
    )


@asynccontextmanager
async def serve_replica_control(
    cluster: Cluster, holder: ConnectionHolder, *, control: LifecycleControl, timing: ControlTiming
) -> AsyncIterator[str]:
    """Serve `holder`'s control listener, and yield the target the Sandbox's runner dials."""
    cluster.fake.tokens[RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid="test-pod-uid",
        audiences=(RUNNER_AUDIENCE,),
    )
    async with serve_runner_control(
        principals=cluster.build_principal_resolver(RUNNER_AUDIENCE),
        sandboxes=cluster.inventory,
        core=k8s_client.CoreV1Api(cluster.api),
        records=ConnectionRecords(cluster.api, namespace=SANDBOX_NAMESPACE, holder=holder),
        control=control,
        timing=timing,
    ) as port:
        yield f"127.0.0.1:{port}"


@dataclass
class Call[Request: Message]:
    """One call the scripted runner received, which the test answers with a response or a status."""

    request: Request
    outcome: asyncio.Future[Message | grpc.StatusCode] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class ScriptedRunner(runner_control_pb2_grpc.RunnerServicer):
    """A `Runner` service that lists `sessions` itself and queues every other call for the test."""

    def __init__(self) -> None:
        self.sessions: dict[str, runner_pb2.SessionSummary] = {}
        # Every call the gateway made, listings included.
        self.received: list[Message] = []
        self._calls: asyncio.Queue[Call[Message]] = asyncio.Queue()

    async def next[Request: Message](self, request_type: type[Request]) -> Call[Request]:
        """The next call the test has not taken yet, which must carry a `request_type`."""
        async with asyncio.timeout(10):
            call = await self._calls.get()
        assert isinstance(call.request, request_type), call.request
        return Call(call.request, call.outcome)

    async def open(self) -> runner_control_pb2.OpenSessionRequest:
        """Answer the next call, an open, with its Session running."""
        call = await self.next(runner_control_pb2.OpenSessionRequest)
        call.outcome.set_result(
            runner_control_pb2.OpenSessionResponse(
                attached=attached(call.request.session_id, runner_pb2.HARNESS_STATE_RUNNING)
            )
        )
        return call.request

    async def _queue[Response: Message](
        self, request: Message, response_type: type[Response], context: grpc.aio.ServicerContext
    ) -> Response:
        self.received.append(request)
        call: Call[Message] = Call(request)
        self._calls.put_nowait(call)
        outcome = await call.outcome
        if isinstance(outcome, grpc.StatusCode):
            await context.abort(outcome, "test runner refuses the call")
        assert isinstance(outcome, response_type), outcome
        return outcome

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        self.received.append(request)
        return runner_pb2.ListSessionsResponse(sessions=self.sessions.values())

    @override
    async def OpenSession(
        self, request: runner_control_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.OpenSessionResponse:
        return await self._queue(request, runner_control_pb2.OpenSessionResponse, context)

    @override
    async def ResumeSession(
        self, request: runner_control_pb2.ResumeSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ResumeSessionResponse:
        return await self._queue(request, runner_control_pb2.ResumeSessionResponse, context)

    @override
    async def Seal(self, request: runner_pb2.SealRequest, context: grpc.aio.ServicerContext) -> runner_pb2.SealResponse:
        return await self._queue(request, runner_pb2.SealResponse, context)

    @override
    async def Quiesce(
        self, request: runner_pb2.QuiesceRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.QuiesceResponse:
        return await self._queue(request, runner_pb2.QuiesceResponse, context)

    @override
    async def DeliverCommand(
        self, request: runner_control_pb2.DeliverCommandRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.DeliverCommandResponse:
        return await self._queue(request, runner_control_pb2.DeliverCommandResponse, context)


@asynccontextmanager
async def connected(
    target: str,
    runner: ScriptedRunner,
    control: LifecycleControl,
    *,
    token: str = RUNNER_TOKEN,
    sandbox_uid: str = SANDBOX_UID,
) -> AsyncIterator[None]:
    """`runner` holds one `Connect` stream to the listener at `target`, which `control` belongs to, as
    the runner `token` names, and serves the gateway's calls on it until the block ends. It does not
    reconnect."""
    client = RunnerControlClient(
        runner, target=target, bearer=token, timing=RUNNER_TIMING, adopt_incarnation=lambda _: None
    )
    async with local_server(
        lambda server: runner_control_pb2_grpc.add_RunnerServicer_to_server(runner, server)
    ) as served:
        connecting = asyncio.create_task(client.connect(served))
        try:
            async with asyncio.timeout(10):
                await control.leases.wait_for(lambda: control.holder(UUID(sandbox_uid)) is not None)
            yield
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
