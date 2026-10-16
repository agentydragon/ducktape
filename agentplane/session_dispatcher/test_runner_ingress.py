"""Queued delivery and shutdown need an acknowledging ingress, not History or Electric.

The real Dispatcher, Gateway reverse tunnel, runner and pinned native harnesses use a
small test-owned ingress. Holding the seal acknowledgement proves that receiving every
entry alone does not permit teardown; the exact sealed cursor must be acknowledged.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, override
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s

from agentplane.background_task import background
from agentplane.grpc.serving import serving
from agentplane.proto import (
    event_log_pb2,
    runner_ingress_pb2,
    runner_ingress_pb2_grpc,
    runner_pb2,
    session_dispatcher_pb2 as dispatcher_pb,
)
from agentplane.runner.config import RunnerConfig
from agentplane.runner.ingress import IngressPusher
from agentplane.runner.runner import Runner
from agentplane.runner.sandbox_launch import SandboxIdentity
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.connection_records import QUIESCE_STAGE_ANNOTATION, ConnectionLeases, ConnectionRecords
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_proxy import Resources, RunnerProxy
from agentplane.runner_gateway.testing.control_listener import HOLDER, RUNNER_TIMING, dialing, serve_runner_control
from agentplane.sandboxes.testing.kubernetes import (
    ACCOUNT,
    DESTINATION,
    OWNER,
    POD_UID,
    SANDBOX,
    SANDBOX_UID,
    SandboxCluster,
)
from agentplane.session_dispatcher.settings import Settings
from agentplane.session_dispatcher.testing.sandbox_status import set_sandbox
from agentplane.session_dispatcher.testing.service import running_dispatcher
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.net import pick_free_port

# gazelle:include_dep //agentplane/runner/testing:fixtures
# gazelle:include_dep @pypi//protobuf
pytest_plugins = ["agentplane.runner.testing.fixtures"]

AUDIENCE = "test-delivery-ingress"
APP_TOKEN = "test-app"
DISPATCHER_TOKEN = "test-dispatcher"
RUNNER_TOKEN = "test-runner"
CONTROLLER_TOKEN = "test-controller"
DISPATCHER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="session-dispatcher")
CONTROLLER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="sandbox-controller")
METADATA = (("authorization", f"Bearer {APP_TOKEN}"),)


class GatedIngress(runner_ingress_pb2_grpc.RunnerIngressServicer):
    """Acknowledge entries immediately; the test explicitly commits the received seal."""

    def __init__(self) -> None:
        self.entries: dict[str, list[event_log_pb2.EventEntry]] = {}
        self.seals: dict[str, runner_pb2.Sealed] = {}
        self.changed = asyncio.Condition()
        self.release_seal = asyncio.Event()

    async def until(self, predicate: Callable[[], bool]) -> None:
        async with self.changed:
            await self.changed.wait_for(predicate)

    @override
    async def Push(  # type: ignore[override]
        self, request_iterator: AsyncIterator[runner_ingress_pb2.PushFrame], context: grpc.aio.ServicerContext
    ) -> None:
        announce = (await anext(request_iterator)).announce
        entries = self.entries.setdefault(announce.session_id, [])
        await context.write(runner_ingress_pb2.Acknowledgement(cursor=len(entries)))
        async for frame in request_iterator:
            if frame.HasField("entry"):
                if frame.entry.cursor <= len(entries):
                    assert entries[frame.entry.cursor - 1] == frame.entry
                else:
                    assert frame.entry.cursor == len(entries) + 1
                    entries.append(frame.entry)
            elif frame.HasField("sealed"):
                assert frame.sealed.cursor == len(entries)
                assert frame.sealed.incarnation_uid == announce.incarnation_uid
                self.seals[announce.session_id] = frame.sealed
            await context.write(runner_ingress_pb2.Acknowledgement(cursor=len(entries)))
            async with self.changed:
                self.changed.notify_all()
            if frame.HasField("sealed"):
                await self.release_seal.wait()
                await context.write(runner_ingress_pb2.Acknowledgement(cursor=frame.sealed.cursor, sealed=True))


async def test_queued_input_and_quiesce_with_test_owned_ingress(
    cluster: SandboxCluster,
    db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: RunnerConfig,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel[Any],
) -> None:
    set_sandbox(cluster, ready=False, incarnation=POD_UID, pod_name=SANDBOX)
    for token, account in (
        (APP_TOKEN, OWNER),
        (DISPATCHER_TOKEN, DISPATCHER),
        (RUNNER_TOKEN, ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=ACCOUNT)),
        (CONTROLLER_TOKEN, CONTROLLER),
    ):
        cluster.fake.tokens[token] = TokenVerdict(
            username=f"system:serviceaccount:{account.namespace}:{account.name}",
            pod_name=SANDBOX,
            pod_uid=POD_UID,
            audiences=(AUDIENCE,),
        )
    dispatcher_token = tmp_path / "dispatcher-token"
    dispatcher_token.write_text(DISPATCHER_TOKEN)
    controller_token = tmp_path / "controller-token"
    controller_token.write_text(CONTROLLER_TOKEN)
    principals = cluster.build_principal_resolver(AUDIENCE)
    control = LifecycleControl(ConnectionLeases())
    proxy = grpc.aio.server()
    proxy.add_generic_rpc_handlers(
        (
            RunnerProxy(
                Resources(
                    principals=principals,
                    caller_accounts=frozenset({DISPATCHER}),
                    sandboxes=cluster.inventory,
                    destinations=DestinationResolver(cluster.inventory, k8s.CoreV1Api(cluster.api)),
                    control=control,
                    sandbox_controller_account=CONTROLLER,
                )
            ),
        )
    )
    ingress = GatedIngress()
    ingress_server = grpc.aio.server()
    runner_ingress_pb2_grpc.add_RunnerIngressServicer_to_server(ingress, ingress_server)
    launch = replace(config.launch, sandbox=SandboxIdentity(namespace=SANDBOX_NAMESPACE, name=SANDBOX, uid=SANDBOX_UID))
    session_id, command_id = str(uuid4()), str(uuid4())
    async with (
        Runner(replace(config, launch=launch)) as runner,
        serving(proxy, "127.0.0.1:0", grace_s=0) as proxy_port,
        serving(ingress_server, "127.0.0.1:0", grace_s=0) as ingress_port,
        background(
            control.leases.watch(k8s.CoordinationV1Api(cluster.api), SANDBOX_NAMESPACE, resync_seconds=60).run(),
            name="connection watch",
        ),
        serve_runner_control(
            principals=principals,
            sandboxes=cluster.inventory,
            core=k8s.CoreV1Api(cluster.api),
            records=ConnectionRecords(cluster.api, namespace=SANDBOX_NAMESPACE, holder=HOLDER),
            control=control,
        ) as control_port,
    ):
        await runner.startup()
        async with (
            dialing(runner, f"127.0.0.1:{control_port}", RUNNER_TOKEN),
            background(
                IngressPusher(
                    runner, target=f"127.0.0.1:{ingress_port}", bearer=RUNNER_TOKEN, timing=RUNNER_TIMING
                ).run(),
                name="runner ingress",
            ),
            asyncio.timeout(60),
        ):
            await control.leases.wait_for(lambda: control.holder(UUID(SANDBOX_UID)) is not None)
            target = f"127.0.0.1:{proxy_port}"
            settings = Settings(
                database_url=db_url,
                namespace=SANDBOX_NAMESPACE,
                caller_accounts=frozenset({OWNER}),
                token_audience=AUDIENCE,
                runner_control_target=target,
                runner_token_file=dispatcher_token,
                runner_request_timeout_s=10,
                request_timeout_s=10,
                host="127.0.0.1",
                port=pick_free_port(),
                _cli_parse_args=False,
            )
            async with running_dispatcher(settings, cluster, monkeypatch) as dispatcher:
                await dispatcher.OpenSession(
                    dispatcher_pb.OpenSessionRequest(sandbox=SANDBOX, session_id=session_id, spec=spec),
                    metadata=METADATA,
                    timeout=10,
                )
                queued = await dispatcher.SubmitInput(
                    dispatcher_pb.SubmitInputRequest(
                        sandbox=SANDBOX, session_id=session_id, command_id=command_id, text="hello without History"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                assert queued.dispatch.state == dispatcher_pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
                assert queued.dispatch.sandbox_uid == SANDBOX_UID
                set_sandbox(cluster, ready=True, incarnation=POD_UID, pod_name=SANDBOX)
                request = await model.request()
                assert "hello without History" in request.user_texts
                await model.reply(request, Text("received without History"))

                def admitted() -> bool:
                    return any(
                        entry.event.HasField("command_admitted")
                        and entry.event.command_admitted.command.command_id == command_id
                        for entry in ingress.entries.get(session_id, [])
                    )

                await ingress.until(admitted)
                admission = next(
                    entry
                    for entry in ingress.entries[session_id]
                    if entry.event.HasField("command_admitted")
                    and entry.event.command_admitted.command.command_id == command_id
                )
                assert admission.event.command_admitted.command.submit_input.text == "hello without History"
                await ingress.until(
                    lambda: any(entry.event.HasField("turn_completed") for entry in ingress.entries[session_id])
                )
                controller = SandboxControlClient(
                    control_target=target, token_file=controller_token, request_timeout_s=10
                )
                await controller.runner_unary(DESTINATION, lambda stub: stub.Quiesce, runner_pb2.QuiesceRequest())
                await ingress.until(lambda: session_id in ingress.seals)
                seal = ingress.seals[session_id]
                assert seal.cursor >= admission.cursor

                async def stage(expected: str) -> None:
                    await cluster.fake.wait_for(
                        lambda: (
                            cluster.fake.objects["leases"]
                            .get(f"{SANDBOX}-runner", {})
                            .get("metadata", {})
                            .get("annotations", {})
                            .get(QUIESCE_STAGE_ANNOTATION)
                            == expected
                        )
                    )

                await stage("flushing")
                flushing = await controller.runner_unary(
                    DESTINATION, lambda stub: stub.Quiesce, runner_pb2.QuiesceRequest()
                )
                assert flushing.stage == runner_pb2.QUIESCE_STAGE_FLUSHING
                ingress.release_seal.set()
                await stage("quiesced")
                quiesced = await controller.runner_unary(
                    DESTINATION, lambda stub: stub.Quiesce, runner_pb2.QuiesceRequest()
                )
                assert quiesced.stage == runner_pb2.QUIESCE_STAGE_QUIESCED


if __name__ == "__main__":
    pytest_bazel.main()
