"""DeleteSandbox's teardown against a scripted runner: the Sandbox is sealed before anything deletes
it, a runner without Seal has its harnesses stopped instead, and holds gate the deletion."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.sandbox_service.commands import db as commands_db
from agentplane.sandbox_service.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.models import SandboxTearingDownError
from agentplane.sandbox_service.protocol_pb2 import Hold, Sandbox, SandboxDestination, TeardownPhase
from agentplane.sandbox_service.session_registry import SessionRegistry
from agentplane.sandbox_service.teardown import Teardown
from agentplane.sandbox_service.testing.backend import seed_runner
from agentplane.sandbox_service.testing.fake_inventory import NAMESPACE, FakeCoreV1Api, FakeCustomObjectsApi
from agentplane.sandbox_service.testing.sessions import register_session
from util.agent_sandbox import SANDBOXES_PLURAL, OperatingMode

# gazelle:include_dep //agentplane/sandbox_service/session_history:conftest
# gazelle:include_dep @pypi//protobuf

pytest_plugins = ("agentplane.sandbox_service.session_history.conftest",)

NAME = "test-torn-down"
LEGACY = "test-legacy-session"


@dataclass
class ScriptedRunner:
    """Seal, ListSessions and an Attach that answers StopRunnerSession by ending the stream."""

    harness_states: dict[str, runner_pb2.HarnessState] = field(default_factory=dict)
    last_cursors: dict[str, int] = field(default_factory=dict)
    # None answers Seal as a runner image without it does.
    seals: dict[str, int] | None = None
    stopped: list[tuple[str, int]] = field(default_factory=list)
    port: int = 0

    async def seal(self, request: runner_pb2.SealRequest, context: grpc.aio.ServicerContext) -> runner_pb2.SealResponse:
        if self.seals is None:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, "test runner predates Seal")
        return runner_pb2.SealResponse(
            sessions=[runner_pb2.SessionSeal(session_id=key, cursor=cursor) for key, cursor in self.seals.items()]
        )

    async def list_sessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        return runner_pb2.ListSessionsResponse(
            sessions=[
                runner_pb2.SessionSummary(session_id=key, harness_state=state, last_cursor=self.last_cursors[key])
                for key, state in self.harness_states.items()
            ]
        )

    async def attach(
        self, requests: AsyncIterator[runner_pb2.ClientMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_pb2.ServerMessage]:
        opened = (await anext(requests)).open
        yield runner_pb2.ServerMessage(
            attached=runner_pb2.Attached(session_id=opened.session_id, last_cursor=self.last_cursors[opened.session_id])
        )
        async for message in requests:
            if message.command.HasField("stop_runner_session"):
                self.stopped.append((opened.session_id, opened.follow.after_cursor))
                self.harness_states[opened.session_id] = runner_pb2.HARNESS_STATE_STOPPED
                return


@pytest.fixture
async def runner() -> AsyncIterator[ScriptedRunner]:
    runner = ScriptedRunner()
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        [
            grpc.method_handlers_generic_handler(
                "ducktape.agentplane.runner.v1.Runner",
                {
                    "Seal": grpc.unary_unary_rpc_method_handler(
                        runner.seal,
                        request_deserializer=runner_pb2.SealRequest.FromString,
                        response_serializer=runner_pb2.SealResponse.SerializeToString,
                    ),
                    "ListSessions": grpc.unary_unary_rpc_method_handler(
                        runner.list_sessions,
                        request_deserializer=runner_pb2.ListSessionsRequest.FromString,
                        response_serializer=runner_pb2.ListSessionsResponse.SerializeToString,
                    ),
                    "Attach": grpc.stream_stream_rpc_method_handler(
                        runner.attach,
                        request_deserializer=runner_pb2.ClientMessage.FromString,
                        response_serializer=runner_pb2.ServerMessage.SerializeToString,
                    ),
                },
            )
        ]
    )
    runner.port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield runner
    finally:
        await server.stop(0)


@pytest.fixture
def destinations(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api, runner: ScriptedRunner
) -> DestinationResolver:
    raw, _ = seed_runner(custom_objects, core_v1, NAME)
    raw["spec"]["operatingMode"] = OperatingMode.SUSPENDED
    return DestinationResolver(inventory, cast(k8s_client.CoreV1Api, core_v1), runner.port)


@pytest.fixture
async def teardown(destinations: DestinationResolver, engine: AsyncEngine) -> Teardown:
    # The registry tables share the history test database; production keeps them apart.
    async with engine.begin() as connection:
        await connection.run_sync(commands_db.Base.metadata.create_all)
    return Teardown(destinations, SessionRegistry(engine), runner_grpc_channel_options={})


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
    runner: ScriptedRunner,
    custom_objects: FakeCustomObjectsApi,
    engine: AsyncEngine,
) -> None:
    public = uuid4()
    await register_session(
        engine,
        public,
        sandbox_namespace=NAMESPACE,
        sandbox_name=NAME,
        sandbox_uid=UUID(sandbox.uid),
        runner_session_id=f"r-{public}",
    )
    runner.seals = {f"r-{public}": 4, LEGACY: 2}
    await inventory.place_hold(NAME, uid=sandbox.uid, session_id=str(public), holder="test")
    assert (sandbox.teardown, sandbox.operating_mode) == (TeardownPhase.TEARDOWN_PHASE_SEALING, OperatingMode.RUNNING)
    # Running to seal, not for new work: Sessions can be followed but not created or commanded.
    with pytest.raises(SandboxTearingDownError):
        await destinations.resolve(destination(sandbox), writes=True)
    await destinations.resolve(destination(sandbox), writes=False)

    await teardown.advance(sandbox)
    sealed = await inventory.get(NAME)
    assert (sealed.teardown, sealed.operating_mode) == (TeardownPhase.TEARDOWN_PHASE_SEALED, OperatingMode.SUSPENDED)
    # Holders name a Session by its public ID, or a legacy one by the runner's own.
    assert await inventory.place_hold(NAME, uid=sandbox.uid, session_id=LEGACY, holder="test") == Hold(
        session_id=LEGACY, holder="test", seal_cursor=2
    )
    await inventory.confirm_hold(NAME, uid=sandbox.uid, session_id=LEGACY, holder="test", through_cursor=2)
    await inventory.confirm_hold(NAME, uid=sandbox.uid, session_id=str(public), holder="test", through_cursor=3)
    await teardown.advance(sealed)
    assert (SANDBOXES_PLURAL, NAME) in custom_objects.objects

    await inventory.confirm_hold(NAME, uid=sandbox.uid, session_id=str(public), holder="test", through_cursor=4)
    await teardown.advance(sealed)
    assert (SANDBOXES_PLURAL, NAME) not in custom_objects.objects


async def test_a_runner_without_seal_has_its_harnesses_stopped(
    sandbox: Sandbox, inventory: SandboxInventory, teardown: Teardown, runner: ScriptedRunner
) -> None:
    runner.harness_states = {LEGACY: runner_pb2.HARNESS_STATE_RUNNING, "test-stopped": runner_pb2.HARNESS_STATE_STOPPED}
    runner.last_cursors = {LEGACY: 3, "test-stopped": 7}
    await teardown.advance(sandbox)
    # Followed from the journal head, so nothing is replayed before the stop.
    assert runner.stopped == [(LEGACY, 3)]
    for session_id, cursor in runner.last_cursors.items():
        hold = await inventory.place_hold(NAME, uid=sandbox.uid, session_id=session_id, holder="test")
        assert hold.seal_cursor == cursor


async def test_an_unreachable_runner_leaves_the_sandbox_unsealed(
    sandbox: Sandbox, inventory: SandboxInventory, teardown: Teardown, core_v1: FakeCoreV1Api
) -> None:
    pod: Any = core_v1.pods[NAME]
    pod.status.conditions = []
    with pytest.raises(DestinationUnavailableError):
        await teardown.advance(sandbox)
    assert (await inventory.get(NAME)).teardown == TeardownPhase.TEARDOWN_PHASE_SEALING


if __name__ == "__main__":
    pytest_bazel.main()
