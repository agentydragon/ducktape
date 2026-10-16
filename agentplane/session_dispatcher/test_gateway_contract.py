"""The public Dispatcher queue and Gateway proxy on the path to a connected Runner."""

import asyncio
from pathlib import Path
from typing import override

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s

from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    runner_control_pb2,
    runner_pb2,
    session_dispatcher_pb2 as dispatcher_pb,
)
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_proxy import Resources as GatewayResources
from agentplane.runner_gateway.testing.control_listener import control_plane
from agentplane.runner_gateway.testing.grpc_service import service as gateway_service
from agentplane.runner_gateway.testing.runner_control import (
    RUNNER_AUDIENCE,
    RUNNER_TOKEN,
    ScriptedRunner,
    attached,
    connected,
)
from agentplane.sandboxes.testing.kubernetes import ACCOUNT, OWNER, POD_UID, SANDBOX, SANDBOX_UID, SandboxCluster
from agentplane.session_dispatcher.settings import Settings
from agentplane.session_dispatcher.testing.sandbox_status import set_sandbox
from agentplane.session_dispatcher.testing.service import running_dispatcher
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.net import pick_free_port

# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf

DISPATCHER_AUDIENCE = "test-dispatcher-gateway-contract"
GATEWAY_AUDIENCE = "test-dispatcher-as-gateway-caller"
DISPATCHER_TOKEN = "test-dispatcher-contract-token"
GATEWAY_CALLER_TOKEN = "test-gateway-contract-caller-token"
DISPATCHER_ACCOUNT = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="session-dispatcher")
SESSION = "contract-session"
COMMAND_ID = "contract-command"
CALLER_METADATA = (("authorization", f"Bearer {DISPATCHER_TOKEN}"),)


class DispatcherGatewayRunner(ScriptedRunner):
    """The Runner's test service with the SubmitInput method used by Dispatcher delivery."""

    @override
    async def SubmitInput(
        self, request: runner_control_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.SubmitInputResponse:
        return await self._queue(request, runner_control_pb2.SubmitInputResponse, context)


async def test_queue_receipt_then_runner_admission_crosses_the_gateway_proxy(
    cluster: SandboxCluster, db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queued sender receipt is distinct from later runner admission through the proxy."""
    set_sandbox(cluster, ready=False, incarnation=POD_UID, pod_name=SANDBOX, running=True)
    cluster.fake.tokens[DISPATCHER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="contract-app",
        pod_uid="contract-app-uid",
        audiences=(DISPATCHER_AUDIENCE,),
    )
    cluster.fake.tokens[GATEWAY_CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{DISPATCHER_ACCOUNT.namespace}:{DISPATCHER_ACCOUNT.name}",
        pod_name="contract-dispatcher",
        pod_uid="contract-dispatcher-uid",
        audiences=(GATEWAY_AUDIENCE,),
    )
    cluster.fake.tokens[RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid=POD_UID,
        audiences=(RUNNER_AUDIENCE,),
    )

    gateway_control = LifecycleControl(ConnectionLeases())
    destinations = DestinationResolver(cluster.inventory, k8s.CoreV1Api(cluster.api))
    gateway_resources = GatewayResources(
        principals=cluster.build_principal_resolver(GATEWAY_AUDIENCE),
        caller_accounts=frozenset({DISPATCHER_ACCOUNT}),
        sandboxes=destinations.sandboxes,
        destinations=destinations,
        control=gateway_control,
        sandbox_controller_account=None,
    )
    gateway_caller_token = tmp_path / "gateway-caller-token"
    gateway_caller_token.write_text(GATEWAY_CALLER_TOKEN)
    runner = DispatcherGatewayRunner()
    async with (
        control_plane(
            gateway_resources, principals=cluster.build_principal_resolver(RUNNER_AUDIENCE), api=cluster.api
        ) as runner_target,
        connected(runner_target, runner, gateway_control),
        gateway_service(gateway_resources) as gateway_target,
    ):
        settings = Settings(
            database_url=db_url,
            namespace=OWNER.namespace,
            caller_accounts=frozenset({OWNER}),
            token_audience=DISPATCHER_AUDIENCE,
            runner_control_target=gateway_target,
            runner_token_file=gateway_caller_token,
            runner_request_timeout_s=5,
            request_timeout_s=5,
            host="127.0.0.1",
            port=pick_free_port(),
            _cli_parse_args=False,
        )
        async with running_dispatcher(settings, cluster, monkeypatch) as dispatcher:
            spec = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, cwd="/state/contract", model="test-model")
            opening = await dispatcher.OpenSession(
                dispatcher_pb.OpenSessionRequest(sandbox=SANDBOX, session_id=SESSION, spec=spec),
                metadata=CALLER_METADATA,
                timeout=5,
            )
            queued = await dispatcher.SubmitInput(
                dispatcher_pb.SubmitInputRequest(
                    command_id=COMMAND_ID, session_id=SESSION, sandbox=SANDBOX, text="contract input"
                ),
                metadata=CALLER_METADATA,
                timeout=5,
            )
            assert opening.opening.state == dispatcher_pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
            assert (queued.dispatch.state, queued.dispatch.sandbox_uid) == (
                dispatcher_pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
                SANDBOX_UID,
            )
            assert runner.received == []

            watch = dispatcher.WatchDispatches(
                dispatcher_pb.WatchDispatchesRequest(filter=dispatcher_pb.DispatchFilter(session_id=SESSION)),
                metadata=CALLER_METADATA,
                timeout=20,
            )
            events = aiter(watch)
            try:
                initial = await anext(events)
                assert initial.dispatch == queued.dispatch

                set_sandbox(cluster, ready=True, incarnation=POD_UID, pod_name=SANDBOX, running=True)
                async with asyncio.timeout(15):
                    open_call = await runner.next(runner_control_pb2.OpenSessionRequest)
                    assert (open_call.request.session_id, open_call.request.spec) == (SESSION, spec)
                    open_call.outcome.set_result(
                        runner_control_pb2.OpenSessionResponse(
                            attached=attached(SESSION, runner_pb2.HARNESS_STATE_RUNNING)
                        )
                    )

                    resume_call = await runner.next(runner_control_pb2.ResumeSessionRequest)
                    assert resume_call.request.session_id == SESSION
                    resume_call.outcome.set_result(
                        runner_control_pb2.ResumeSessionResponse(
                            attached=attached(SESSION, runner_pb2.HARNESS_STATE_RUNNING)
                        )
                    )

                    submit_call = await runner.next(runner_control_pb2.SubmitInputRequest)
                    expected_command = command_pb2.Command(
                        command_id=COMMAND_ID, submit_input=command_pb2.SubmitInput(text="contract input")
                    )
                    assert submit_call.request == runner_control_pb2.SubmitInputRequest(
                        session_id=SESSION, command_id=COMMAND_ID, text="contract input"
                    )
                    admission = event_log_pb2.EventEntry(cursor=1)
                    admission.event.command_admitted.command.CopyFrom(expected_command)
                    submit_call.outcome.set_result(runner_control_pb2.SubmitInputResponse(admission=admission))

                    while True:
                        changed = await anext(events)
                        if (
                            changed.dispatch.command_id == COMMAND_ID
                            and changed.dispatch.state == dispatcher_pb.DELIVERY_STATE_RUNNER_ADMITTED
                        ):
                            admitted = changed.dispatch
                            break
            finally:
                watch.cancel()

            assert admitted.sandbox_uid == SANDBOX_UID
            assert admitted.admission == admission
            repeated = await dispatcher.SubmitInput(
                dispatcher_pb.SubmitInputRequest(
                    command_id=COMMAND_ID, session_id=SESSION, sandbox=SANDBOX, text="contract input"
                ),
                metadata=CALLER_METADATA,
                timeout=5,
            )
            assert repeated.dispatch == admitted
            assert sum(isinstance(request, runner_control_pb2.SubmitInputRequest) for request in runner.received) == 1


if __name__ == "__main__":
    pytest_bazel.main()
