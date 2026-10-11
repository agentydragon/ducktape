"""DeleteSandbox's teardown against a scripted Runner Gateway: the Sandbox is sealed before anything
deletes it, and holds gate the deletion unless the runner pushes to an ingress, which gates it on every
seal being flushed instead."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast, override

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.grpc_serving import serving
from agentplane.proto import runner_gateway_pb2_grpc, runner_pb2
from agentplane.proto.sandbox_service_pb2 import Hold, Sandbox, SandboxDestination, SandboxRequest, TeardownPhase
from agentplane.sandbox_service.client import ServiceError
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.models import SandboxTearingDownError
from agentplane.sandbox_service.teardown import GatewaySealing, SealNotFlushedError, Teardown
from agentplane.sandbox_service.testing.backend import seed_runner
from agentplane.sandbox_service.testing.fake_inventory import FakeCoreV1Api, FakeCustomObjectsApi
from agentplane.sandbox_service.testing.grpc_service import connect
from util.agent_sandbox import SANDBOXES_PLURAL, OperatingMode

# gazelle:include_dep @pypi//protobuf

NAME = "test-torn-down"


@dataclass
class ScriptedGateway(runner_gateway_pb2_grpc.RunnerGatewayServicer):
    """SealSandbox answers `seal`, already named as holders name each Session; while `seal` is None
    it answers UNAVAILABLE, as for a runner nothing can reach."""

    seal: runner_pb2.SealResponse | None = None

    @override
    async def SealSandbox(self, request: SandboxRequest, context: grpc.aio.ServicerContext) -> runner_pb2.SealResponse:
        if self.seal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "test runner unreachable")
        return self.seal


def sealed(
    cursors: dict[str, int], *, pushes: bool = False, flushed: frozenset[str] = frozenset()
) -> runner_pb2.SealResponse:
    return runner_pb2.SealResponse(
        sessions=[
            runner_pb2.SessionSeal(session_id=key, cursor=cursor, flushed=key in flushed)
            for key, cursor in cursors.items()
        ],
        pushes=pushes,
    )


@pytest.fixture
def gateway() -> ScriptedGateway:
    return ScriptedGateway()


@pytest.fixture
async def teardown(inventory: SandboxInventory, gateway: ScriptedGateway, token_file: Path) -> AsyncIterator[Teardown]:
    server = grpc.aio.server()
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(gateway, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        client = connect(
            f"127.0.0.1:{port}", token_file, namespace=inventory.namespace, command_admission_timeout_s=None
        )
        try:
            yield Teardown(inventory, GatewaySealing(client))
        finally:
            await client.close()


@pytest.fixture
def destinations(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> DestinationResolver:
    raw, _ = seed_runner(custom_objects, core_v1, NAME)
    raw["spec"]["operatingMode"] = OperatingMode.SUSPENDED
    return DestinationResolver(inventory, cast(k8s_client.CoreV1Api, core_v1), 7000)


@pytest.fixture
async def sandbox(inventory: SandboxInventory, destinations: DestinationResolver) -> Sandbox:
    view = await inventory.get(NAME)
    await inventory.begin_teardown(NAME, uid=view.uid)
    return await inventory.get(NAME)


def destination(sandbox: Sandbox) -> SandboxDestination:
    return SandboxDestination(owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid)


async def test_sealed_sessions_hold_the_deletion_until_confirmed(
    sandbox: Sandbox,
    inventory: SandboxInventory,
    destinations: DestinationResolver,
    teardown: Teardown,
    gateway: ScriptedGateway,
    custom_objects: FakeCustomObjectsApi,
) -> None:
    gateway.seal = sealed({"test-public": 4, "test-legacy": 2})
    await inventory.place_hold(NAME, uid=sandbox.uid, session_id="test-public", holder="test")
    assert (sandbox.teardown, sandbox.operating_mode) == (TeardownPhase.TEARDOWN_PHASE_SEALING, OperatingMode.RUNNING)
    # Running to seal, not for new work: Sessions can be followed but not created or commanded.
    with pytest.raises(SandboxTearingDownError):
        await destinations.resolve(destination(sandbox), writes=True)
    await destinations.resolve(destination(sandbox), writes=False)

    await teardown.advance(sandbox)
    view = await inventory.get(NAME)
    # Still running: the holders follow their Sessions to the seal through the runner.
    assert (view.teardown, view.operating_mode) == (TeardownPhase.TEARDOWN_PHASE_SEALED, OperatingMode.RUNNING)
    await destinations.resolve(destination(sandbox), writes=False)
    assert await inventory.place_hold(NAME, uid=sandbox.uid, session_id="test-legacy", holder="test") == Hold(
        session_id="test-legacy", holder="test", seal_cursor=2
    )
    await inventory.confirm_hold(NAME, uid=sandbox.uid, session_id="test-legacy", holder="test", through_cursor=2)
    await inventory.confirm_hold(NAME, uid=sandbox.uid, session_id="test-public", holder="test", through_cursor=3)
    await teardown.advance(view)
    assert (SANDBOXES_PLURAL, NAME) in custom_objects.objects

    await inventory.confirm_hold(NAME, uid=sandbox.uid, session_id="test-public", holder="test", through_cursor=4)
    await teardown.advance(view)
    assert (SANDBOXES_PLURAL, NAME) not in custom_objects.objects


async def test_a_suspended_sealed_sandbox_runs_again_until_its_holds_confirm(
    sandbox: Sandbox,
    inventory: SandboxInventory,
    teardown: Teardown,
    gateway: ScriptedGateway,
    custom_objects: FakeCustomObjectsApi,
) -> None:
    # A holder confirms by following its Session to the seal, which needs a running runner.
    gateway.seal = sealed({"test-held": 4})
    await inventory.place_hold(NAME, uid=sandbox.uid, session_id="test-held", holder="test")
    await teardown.advance(sandbox)
    custom_objects.objects[(SANDBOXES_PLURAL, NAME)]["spec"]["operatingMode"] = OperatingMode.SUSPENDED
    await teardown.advance(await inventory.get(NAME))
    assert (await inventory.get(NAME)).operating_mode == OperatingMode.RUNNING

    await inventory.confirm_hold(NAME, uid=sandbox.uid, session_id="test-held", holder="test", through_cursor=4)
    await teardown.advance(await inventory.get(NAME))
    assert (SANDBOXES_PLURAL, NAME) not in custom_objects.objects


async def test_a_pushing_runner_is_deleted_once_every_seal_is_flushed(
    sandbox: Sandbox,
    inventory: SandboxInventory,
    teardown: Teardown,
    gateway: ScriptedGateway,
    custom_objects: FakeCustomObjectsApi,
) -> None:
    cursors = {"test-flushed": 4, "test-unflushed": 2}
    gateway.seal = sealed(cursors, pushes=True, flushed=frozenset({"test-flushed"}))
    with pytest.raises(SealNotFlushedError):
        await teardown.advance(sandbox)
    assert (await inventory.get(NAME)).teardown == TeardownPhase.TEARDOWN_PHASE_SEALING

    # History holds every Session through its seal, so an unconfirmed hold keeps nothing.
    await inventory.place_hold(NAME, uid=sandbox.uid, session_id="test-unflushed", holder="test")
    gateway.seal = sealed(cursors, pushes=True, flushed=frozenset(cursors))
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
