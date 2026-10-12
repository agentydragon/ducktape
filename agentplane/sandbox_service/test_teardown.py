"""DeleteSandbox's teardown against a scripted Runner Gateway: the Sandbox is sealed before anything
deletes it, and deleted once its runner's ingress has acknowledged every seal."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast, override

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.grpc_calls import ServiceError
from agentplane.grpc_serving import serving
from agentplane.proto import runner_gateway_pb2, runner_gateway_pb2_grpc, runner_pb2
from agentplane.proto.sandbox_service_pb2 import Sandbox, SandboxDestination, TeardownPhase
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.testing.grpc_service import control_client
from agentplane.sandbox_service.errors import SandboxTearingDownError
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.teardown import GatewaySealing, SealNotFlushedError, Teardown
from agentplane.sandbox_service.testing.backend import seed_runner
from agentplane.sandbox_service.testing.fake_inventory import FakeCoreV1Api, FakeCustomObjectsApi
from util.agent_sandbox import SANDBOXES_PLURAL, OperatingMode

# gazelle:include_dep @pypi//protobuf

NAME = "test-torn-down"


@dataclass
class ScriptedGateway(runner_gateway_pb2_grpc.RunnerGatewayServicer):
    """SealSandbox answers `seal`; while it is None it answers UNAVAILABLE, as for a runner nothing
    can reach."""

    seal: runner_gateway_pb2.SealSandboxResponse | None = None

    @override
    async def SealSandbox(
        self, request: runner_gateway_pb2.SealSandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.SealSandboxResponse:
        if self.seal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "test runner unreachable")
        return self.seal


def sealed(cursors: dict[str, int], *, flushed: frozenset[str]) -> runner_gateway_pb2.SealSandboxResponse:
    return runner_gateway_pb2.SealSandboxResponse(
        sessions=[
            runner_pb2.SessionSeal(session_id=key, cursor=cursor, flushed=key in flushed)
            for key, cursor in cursors.items()
        ]
    )


@pytest.fixture
def gateway() -> ScriptedGateway:
    return ScriptedGateway()


@pytest.fixture
async def teardown(inventory: SandboxInventory, gateway: ScriptedGateway, token_file: Path) -> AsyncIterator[Teardown]:
    server = grpc.aio.server()
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(gateway, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield Teardown(inventory, GatewaySealing(control_client(token_file, f"127.0.0.1:{port}")))


@pytest.fixture
def destinations(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> DestinationResolver:
    raw, _ = seed_runner(custom_objects, core_v1, NAME)
    raw["spec"]["operatingMode"] = OperatingMode.SUSPENDED
    return DestinationResolver(inventory, cast(k8s_client.CoreV1Api, core_v1))


@pytest.fixture
async def sandbox(inventory: SandboxInventory, destinations: DestinationResolver) -> Sandbox:
    view = await inventory.get(NAME)
    await inventory.begin_teardown(NAME, uid=view.uid)
    return await inventory.get(NAME)


def destination(sandbox: Sandbox) -> SandboxDestination:
    return SandboxDestination(owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid)


async def test_a_sandbox_is_deleted_once_every_seal_is_flushed(
    sandbox: Sandbox,
    inventory: SandboxInventory,
    destinations: DestinationResolver,
    teardown: Teardown,
    gateway: ScriptedGateway,
    custom_objects: FakeCustomObjectsApi,
) -> None:
    assert (sandbox.teardown, sandbox.operating_mode) == (TeardownPhase.TEARDOWN_PHASE_SEALING, OperatingMode.RUNNING)
    # Running to seal, not for new work: Sessions can be read but not created or commanded.
    with pytest.raises(SandboxTearingDownError):
        await destinations.resolve(destination(sandbox), writes=True)
    await destinations.resolve(destination(sandbox), writes=False)

    cursors = {"test-flushed": 4, "test-unflushed": 2}
    gateway.seal = sealed(cursors, flushed=frozenset({"test-flushed"}))
    with pytest.raises(SealNotFlushedError):
        await teardown.advance(sandbox)
    assert (await inventory.get(NAME)).teardown == TeardownPhase.TEARDOWN_PHASE_SEALING

    gateway.seal = sealed(cursors, flushed=frozenset(cursors))
    await teardown.advance(await inventory.get(NAME))
    assert (SANDBOXES_PLURAL, NAME) not in custom_objects.objects


async def test_an_unreachable_runner_leaves_the_sandbox_unsealed(
    sandbox: Sandbox, inventory: SandboxInventory, teardown: Teardown
) -> None:
    with pytest.raises(ServiceError) as refused:
        await teardown.advance(sandbox)
    assert refused.value.code == grpc.StatusCode.UNAVAILABLE
    assert (await inventory.get(NAME)).teardown == TeardownPhase.TEARDOWN_PHASE_SEALING


if __name__ == "__main__":
    pytest_bazel.main()
