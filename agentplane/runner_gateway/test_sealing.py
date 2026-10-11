"""Teardown's seal against a scripted runner reached through its inbound API: each Session is named as
retention holders name it, and a runner without Seal has its harnesses stopped instead."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import override
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.proto import runner_pb2
from agentplane.runner.testing.runner_server import UnimplementedRunner, serve_runner
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.sealing import Sealing
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.runner_gateway.testing.sessions import register_session
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.testing.kubernetes import SANDBOX, SANDBOX_UID, Cluster
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE

# gazelle:include_dep @pypi//protobuf

LEGACY = "test-legacy-session"


@dataclass
class ScriptedRunner(UnimplementedRunner):
    """Seal, ListSessions and an Attach that answers StopRunnerSession by ending the stream."""

    harness_states: dict[str, runner_pb2.HarnessState] = field(default_factory=dict)
    last_cursors: dict[str, int] = field(default_factory=dict)
    # None answers Seal as a runner image without it does.
    seals: dict[str, int] | None = None
    stopped: list[tuple[str, int]] = field(default_factory=list)
    port: int = 0

    @override
    async def Seal(self, request: runner_pb2.SealRequest, context: grpc.aio.ServicerContext) -> runner_pb2.SealResponse:
        if self.seals is None:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, "test runner predates Seal")
        return runner_pb2.SealResponse(
            sessions=[runner_pb2.SessionSeal(session_id=key, cursor=cursor) for key, cursor in self.seals.items()]
        )

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        return runner_pb2.ListSessionsResponse(
            sessions=[
                runner_pb2.SessionSummary(session_id=key, harness_state=state, last_cursor=self.last_cursors[key])
                for key, state in self.harness_states.items()
            ]
        )

    @override
    async def Attach(
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
    async with serve_runner(runner) as port:
        runner.port = port
        yield runner


@pytest.fixture
def sealing(cluster: Cluster, engine: AsyncEngine, runner: ScriptedRunner) -> Sealing:
    return Sealing(
        DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api), runner.port),
        SessionRegistry(engine),
        connections=ConnectionStore(engine),
        control=LifecycleControl(ConnectionLeases()),
        runner_grpc_channel_options={},
    )


async def test_seals_are_named_as_holders_name_each_session(
    sealing: Sealing, cluster: Cluster, engine: AsyncEngine, runner: ScriptedRunner
) -> None:
    public = uuid4()
    await register_session(
        engine,
        public,
        sandbox_namespace=SANDBOX_NAMESPACE,
        sandbox_name=SANDBOX,
        sandbox_uid=UUID(SANDBOX_UID),
        runner_session_id=f"r-{public}",
    )
    runner.seals = {f"r-{public}": 4, LEGACY: 2}
    sealed = await sealing.seal(await cluster.inventory.get(SANDBOX))
    # Holders name a Session by its public ID, or a legacy one by the runner's own.
    assert {(seal.session_id, seal.cursor) for seal in sealed.sessions} == {(str(public), 4), (LEGACY, 2)}


async def test_a_runner_without_seal_has_its_harnesses_stopped(
    sealing: Sealing, cluster: Cluster, runner: ScriptedRunner
) -> None:
    runner.harness_states = {LEGACY: runner_pb2.HARNESS_STATE_RUNNING, "test-stopped": runner_pb2.HARNESS_STATE_STOPPED}
    runner.last_cursors = {LEGACY: 3, "test-stopped": 7}
    sealed = await sealing.seal(await cluster.inventory.get(SANDBOX))
    # Followed from the journal head, so nothing is replayed before the stop.
    assert runner.stopped == [(LEGACY, 3)]
    assert {(seal.session_id, seal.cursor) for seal in sealed.sessions} == set(runner.last_cursors.items())


if __name__ == "__main__":
    pytest_bazel.main()
