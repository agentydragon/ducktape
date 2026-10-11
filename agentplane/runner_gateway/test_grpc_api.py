"""Real gRPC and TokenReview, with a controllable runner peer for transport failure edges."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import override
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import MessageToDict, ParseDict
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.grpc_serving import serving
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2, runner_pb2_grpc, sandbox_service_pb2
from agentplane.proto.sandbox_service_pb2 import ResolvedGrant, SandboxDestination
from agentplane.runner.errors import RunnerError
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.commands.store import CommandSubmission, SubmissionState, SubmissionStore
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.runner_gateway.testing.grpc_service import service_client
from agentplane.runner_gateway.testing.journal import admission, entry
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.runner_gateway.testing.sessions import register_session
from agentplane.sandbox_service import wire
from agentplane.sandbox_service.client import SandboxServiceClient, ServiceError
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.kubernetes_grants import RoleBindingGrant, RoleRef
from agentplane.sandbox_service.kubernetes_views import (
    KUBERNETES_GRANTS_ANNOTATION,
    KUBERNETES_GRANTS_READY_ANNOTATION,
    PROVISIONING_ANNOTATION,
)
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, DESTINATION, OWNER, SANDBOX, SANDBOX_UID, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.agent_sandbox import SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

AUDIENCE = "test-sandbox-service"


@dataclass
class PeerAttachment:
    opened: runner_pb2.Open
    commands: asyncio.Queue[runner_pb2.ClientMessage] = field(default_factory=asyncio.Queue)
    responses: asyncio.Queue[runner_pb2.ServerMessage | grpc.StatusCode | None] = field(default_factory=asyncio.Queue)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class Peer(runner_pb2_grpc.RunnerServicer):
    def __init__(self) -> None:
        self.attachments: asyncio.Queue[PeerAttachment] = asyncio.Queue()
        self.port = 0
        self.state = runner_pb2.HARNESS_STATE_RUNNING
        self.setup_state = runner_pb2.SETUP_STATE_NOT_REQUIRED
        self.answer_open = True
        self.opened_ids: dict[str, runner_pb2.Open] = {}
        self.history: dict[str, list[event_log_pb2.EventEntry]] = {}
        # Replay pauses after this cursor until released, as a slow journal read would.
        self.replay_paused_after: int | None = None
        self.replay_released = asyncio.Event()

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        return runner_pb2.ListSessionsResponse(
            sessions=[
                runner_pb2.SessionSummary(
                    session_id=key,
                    spec=value.spec,
                    last_cursor=len(self.history.get(key, [])),
                    harness_state=self.state,
                    setup_state=self.setup_state,
                )
                for key, value in self.opened_ids.items()
            ]
        )

    @override
    async def Attach(
        self, requests: AsyncIterator[runner_pb2.ClientMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_pb2.ServerMessage]:
        first = await anext(requests)
        connection = PeerAttachment(first.open)
        if first.open.HasField("spec"):
            self.opened_ids[first.open.session_id] = first.open
        self.attachments.put_nowait(connection)

        async def consume() -> None:
            async for request in requests:
                connection.commands.put_nowait(request)

        consumer = asyncio.create_task(consume())
        try:
            if self.answer_open:
                yield runner_pb2.ServerMessage(
                    attached=runner_pb2.Attached(
                        session_id=first.open.session_id,
                        harness_state=self.state,
                        last_cursor=len(self.history.get(first.open.session_id, [])),
                    )
                )
                for entry in self.history.get(first.open.session_id, []):
                    if entry.cursor > first.open.follow.after_cursor:
                        if self.replay_paused_after is not None and entry.cursor > self.replay_paused_after:
                            await self.replay_released.wait()
                        yield runner_pb2.ServerMessage(event_entry=entry)
            while (message := await connection.responses.get()) is not None:
                if isinstance(message, grpc.StatusCode):
                    await context.abort(message, "test runner failure")
                else:
                    yield message
        finally:
            consumer.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await consumer
            finally:
                # Aborting the server RPC can also fail its request-reader task. Still record
                # completed cleanup, rather than making the test wait on an unreachable marker.
                connection.closed.set()


@pytest.fixture
async def peer() -> AsyncIterator[Peer]:
    peer = Peer()
    server = grpc.aio.server()
    runner_pb2_grpc.add_RunnerServicer_to_server(peer, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        peer.port = port
        yield peer


async def register(engine: AsyncEngine, session_id: UUID, runner_session_id: str) -> None:
    """Register a Session on the fake cluster's Sandbox."""
    await register_session(
        engine,
        session_id,
        sandbox_namespace=SANDBOX_NAMESPACE,
        sandbox_name=SANDBOX,
        sandbox_uid=UUID(SANDBOX_UID),
        runner_session_id=runner_session_id,
    )


@pytest.fixture
def resources(cluster: Cluster, peer: Peer, engine: AsyncEngine) -> Resources:
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="test-caller",
        pod_uid="test-caller-uid",
        audiences=(AUDIENCE,),
    )
    return replace(
        build_resources(
            engine,
            principals=cluster.build_principal_resolver(AUDIENCE),
            destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api), peer.port),
            caller_accounts=frozenset({OWNER}),
            platform_instructions="Test guidance",
        ),
        admission_timeout_s=1,
    )


@pytest.fixture
async def remote(cluster: Cluster, resources: Resources, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    async with service_client(resources, token_file, cluster.provisioning) as client:
        yield client


async def test_managed_open_uses_the_client_session_id_and_retries_frozen_launch(
    cluster: Cluster,
    resources: Resources,
    token_file: Path,
    peer: Peer,
    engine: AsyncEngine,
    sessions: SessionRegistry,
    submissions: SubmissionStore,
) -> None:
    async with service_client(resources, token_file, cluster.provisioning) as remote:
        runner = remote.runner(DESTINATION)
        overrides: dict[str, object] = {"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/state/{session_id}"}
        requested = uuid4()
        created = await runner.create(session_id=requested, spec=overrides)
        first = await peer.attachments.get()
        public = created.session_id
        assert public == str(requested)
        assert created.attached.session_id == public
        # The runner's Session ID is the client's, as the ingress announces it.
        assert first.opened.session_id == public
        assert first.opened.spec.cwd == f"/state/{public}"
        assert public in first.opened.spec.instructions
        await first.closed.wait()
        assert [row.session_id for row in await runner.list_sessions()] == [public]

        # Simulate changed defaults/platform instructions after the original reply was lost.
        async with service_client(
            replace(resources, platform_instructions="Changed guidance"), token_file, cluster.provisioning
        ) as retry:
            again = await retry.runner(DESTINATION).create(session_id=requested, spec=overrides)
        second = await peer.attachments.get()
        assert again == created
        assert second.opened == first.opened
        assert "Changed guidance" not in second.opened.spec.instructions
        await second.closed.wait()

        with pytest.raises(ServiceError) as conflict:
            await runner.create(session_id=requested, spec=overrides | {"model": "different"})
        assert conflict.value.code == grpc.StatusCode.INVALID_ARGUMENT
        assert peer.attachments.empty()

        command = command_pb2.Command(command_id="managed-command", submit_input=command_pb2.SubmitInput(text="hi"))
        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(runner.command(public, command, after_cursor=0))
            admitted = await peer.attachments.get()
            assert admitted.opened.session_id == public
            assert (await admitted.commands.get()).command == command
            receipt = admission(command, 1)
            admitted.responses.put_nowait(runner_pb2.ServerMessage(event_entry=receipt))
            assert await pending == receipt
        await admitted.closed.wait()

        assert await runner.resume(public) == created.attached
        resumed = await peer.attachments.get()
        assert resumed.opened.session_id == public
        await resumed.closed.wait()


async def test_public_session_command_survives_a_lost_reply(
    cluster: Cluster,
    resources: Resources,
    token_file: Path,
    peer: Peer,
    engine: AsyncEngine,
    submissions: SubmissionStore,
) -> None:
    session_id = uuid4()
    physical = f"r-{session_id}"
    await register(engine, session_id, physical)
    peer.opened_ids[physical] = runner_pb2.Open(session_id=physical)
    command = command_pb2.Command(command_id="durable", submit_input=command_pb2.SubmitInput(text="hi"))
    async with asyncio.timeout(10), service_client(resources, token_file, cluster.provisioning) as remote:
        runner = remote.runner(DESTINATION)
        with pytest.raises(TimeoutError):
            await runner.command(str(session_id), command, after_cursor=0)
        lost = await peer.attachments.get()
        assert (await lost.commands.get()).command == command
        await lost.closed.wait()
        # The runner did journal it, but nothing reads its journal: the submission stays pending.
        assert (await submissions.read(session_id, "durable")).state == SubmissionState.PENDING_ADMISSION

        # The caller's retry delivers it again, and the runner's original receipt settles it.
        receipt = admission(command, 1)
        peer.history[physical] = [receipt]
        assert await runner.command(str(session_id), command, after_cursor=0) == receipt
        redelivered = await peer.attachments.get()
        assert (await redelivered.commands.get()).command == command
        await redelivered.closed.wait()
        assert (await submissions.read(session_id, "durable")).state == SubmissionState.ADMITTED

        assert await runner.command(str(session_id), command, after_cursor=0) == receipt
        assert peer.attachments.empty()  # an admitted retry needs no runner
        changed = command_pb2.Command(command_id="durable", submit_input=command_pb2.SubmitInput(text="changed"))
        with pytest.raises(ServiceError) as conflict:
            await runner.command(str(session_id), changed, after_cursor=0)
        assert conflict.value.code == grpc.StatusCode.ALREADY_EXISTS


async def test_command_status_reads_retained_submissions_without_the_runner(
    cluster: Cluster,
    resources: Resources,
    token_file: Path,
    peer: Peer,
    engine: AsyncEngine,
    sessions: SessionRegistry,
    submissions: SubmissionStore,
) -> None:
    session_id = uuid4()
    await register(engine, session_id, f"r-{session_id}")
    commands = {
        name: command_pb2.Command(command_id=name, submit_input=command_pb2.SubmitInput(text=name))
        for name in ("admitted", "pending", "rejected")
    }
    for command in commands.values():
        await submissions.accept(session_id, CommandSubmission(command=command), caller=OWNER)
    receipt = admission(commands["admitted"], 1)
    await submissions.record_admission(session_id, receipt)
    await submissions.record_rejection(session_id, "rejected", "test refusal")
    destination = sandbox_service_pb2.SessionDestination(sandbox=DESTINATION, session_id=str(session_id))
    async with service_client(
        replace(resources, sessions=sessions, submissions=submissions), token_file, cluster.provisioning
    ) as remote:

        async def get(
            command_id: str, at: sandbox_service_pb2.SessionDestination = destination
        ) -> sandbox_service_pb2.CommandStatus:
            return await remote.unary(
                remote.gateway.GetCommand, sandbox_service_pb2.GetCommandRequest(destination=at, command_id=command_id)
            )

        async def page(request: sandbox_service_pb2.ListCommandsRequest) -> sandbox_service_pb2.ListCommandsResponse:
            request.destination.CopyFrom(destination)
            return await remote.unary(remote.gateway.ListCommands, request)

        admitted = await get("admitted")
        assert (admitted.command, admitted.state, admitted.admission) == (
            commands["admitted"],
            sandbox_service_pb2.COMMAND_STATE_ADMITTED,
            receipt,
        )
        pending = await get("pending")
        assert pending.state == sandbox_service_pb2.COMMAND_STATE_PENDING_ADMISSION
        assert not pending.HasField("admission")
        rejected = await get("rejected")
        assert (rejected.state, rejected.rejection) == (sandbox_service_pb2.COMMAND_STATE_REJECTED, "test refusal")

        listed: list[sandbox_service_pb2.CommandStatus] = []
        token = ""
        while True:
            response = await page(sandbox_service_pb2.ListCommandsRequest(page_token=token, page_size=2))
            listed.extend(response.commands)
            if not (token := response.next_page_token):
                break
        assert listed == [admitted, pending, rejected]
        only_pending = await page(
            sandbox_service_pb2.ListCommandsRequest(states=[sandbox_service_pb2.COMMAND_STATE_PENDING_ADMISSION])
        )
        assert list(only_pending.commands) == [pending]

        other_incarnation = sandbox_service_pb2.SessionDestination()
        other_incarnation.CopyFrom(destination)
        other_incarnation.sandbox.sandbox_uid = str(uuid4())
        legacy = sandbox_service_pb2.SessionDestination(sandbox=DESTINATION, session_id="legacy-runner-session")
        for command_id, at, code in (
            ("unknown", destination, grpc.StatusCode.NOT_FOUND),
            ("pending", other_incarnation, grpc.StatusCode.NOT_FOUND),
            ("pending", legacy, grpc.StatusCode.INVALID_ARGUMENT),
        ):
            with pytest.raises(ServiceError) as refused:
                await get(command_id, at)
            assert refused.value.code == code
    assert peer.attachments.empty()


async def test_control_routed_command_never_falls_back_to_the_relay(
    cluster: Cluster,
    resources: Resources,
    token_file: Path,
    peer: Peer,
    engine: AsyncEngine,
    sessions: SessionRegistry,
    submissions: SubmissionStore,
) -> None:
    # A runner agreed commands once, and no connection holds the Sandbox's Lease now.
    await ConnectionStore(engine).record_operations(UUID(SANDBOX_UID), {"commands"})
    session_id = uuid4()
    await register(engine, session_id, f"r-{session_id}")
    command = command_pb2.Command(command_id="channel", submit_input=command_pb2.SubmitInput(text="hi"))
    async with service_client(resources, token_file, cluster.provisioning) as remote:
        runner = remote.runner(DESTINATION)
        for session in (str(session_id), "legacy-runner-session"):
            with pytest.raises(ServiceError) as unavailable:
                await runner.command(session, command, after_cursor=0)
            assert unavailable.value.code == grpc.StatusCode.UNAVAILABLE
    assert peer.attachments.empty()
    assert (await submissions.read(session_id, "channel")).state == SubmissionState.PENDING_ADMISSION


async def test_lookup_open_scopes_reservation_and_waits_for_runner_confirmation(
    cluster: Cluster, resources: Resources, token_file: Path, peer: Peer, engine: AsyncEngine, sessions: SessionRegistry
) -> None:
    async with service_client(resources, token_file, cluster.provisioning) as remote:
        runner = remote.runner(DESTINATION)
        assert not (await runner.lookup(idempotency_key="lookup-key")).session_id
        reserved = await sessions.reserve(
            session_id=None,
            caller_namespace=OWNER.namespace,
            caller_name=OWNER.name,
            sandbox_namespace=SANDBOX_NAMESPACE,
            sandbox_name=SANDBOX,
            sandbox_uid=UUID(SANDBOX_UID),
            open_key="lookup-key",
            open_request=b"original inputs remain private",
            launch_spec=lambda _: b"private launch",
        )
        pending = await runner.lookup(idempotency_key="lookup-key")
        assert pending.session_id == str(reserved.session_id)
        assert not pending.HasField("summary")  # reservation alone is not an Open success
        with pytest.raises(ServiceError) as wrong_incarnation:
            await remote.runner(
                SandboxDestination(owner=DESTINATION.owner, sandbox=SANDBOX, sandbox_uid=str(uuid4()))
            ).lookup(idempotency_key="lookup-key")
        assert wrong_incarnation.value.code == grpc.StatusCode.NOT_FOUND
        assert not (await runner.lookup(idempotency_key="other-key")).session_id
        with pytest.raises(ServiceError) as invalid:
            await runner.lookup(idempotency_key="")
        assert invalid.value.code == grpc.StatusCode.INVALID_ARGUMENT

        requested = uuid4()
        created = await runner.create(
            session_id=requested, spec={"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/state"}
        )
        await (await peer.attachments.get()).closed.wait()
        confirmed = await runner.lookup(idempotency_key=str(requested))
        assert confirmed.session_id == created.session_id == str(requested)
        assert confirmed.HasField("summary")
        assert confirmed.summary.session_id == created.session_id
        assert confirmed.summary.spec.model == "test-model"


async def test_lookup_rejects_failed_native_open_but_recovers_an_earlier_success(
    cluster: Cluster, resources: Resources, token_file: Path, peer: Peer, engine: AsyncEngine, sessions: SessionRegistry
) -> None:
    """Inventory also retains a spec after a native handshake fails; never project it as a Thread."""
    async with service_client(resources, token_file, cluster.provisioning) as remote:
        runner = remote.runner(DESTINATION)
        requested = uuid4()
        created = await runner.create(
            session_id=requested, spec={"harness": "HARNESS_CODEX", "model": "m", "cwd": "/state"}
        )
        await (await peer.attachments.get()).closed.wait()
        physical_id = str(requested)
        peer.state = runner_pb2.HARNESS_STATE_STOPPED

        # A failed native handshake leaves the runner's session in inventory but
        # never emits HarnessStarted. The observation-only attachment does not retry Open.
        peer.history[physical_id] = [entry(1, event_pb2.Event(harness_exited=event_pb2.HarnessExited(exit_code=1)))]
        failed = await runner.lookup(idempotency_key=str(requested))
        assert failed.session_id == created.session_id
        assert failed.failed
        assert not failed.HasField("summary")
        observed = await peer.attachments.get()
        assert observed.opened.session_id == physical_id
        assert not observed.opened.HasField("spec")
        await observed.closed.wait()

        # A reservation with an in-flight setup has neither success nor failure evidence.
        peer.history[physical_id] = [entry(1, event_pb2.Event(setup_started=event_pb2.SetupStarted()))]
        peer.setup_state = runner_pb2.SETUP_STATE_RUNNING
        pending = await runner.lookup(idempotency_key=str(requested))
        assert not pending.failed
        assert not pending.HasField("summary")
        await (await peer.attachments.get()).closed.wait()

        # A previously healthy session may be stopped now. Historical native
        # startup proves Open succeeded, even when a later exit stopped it.
        peer.setup_state = runner_pb2.SETUP_STATE_NOT_REQUIRED
        peer.history[physical_id] = [
            entry(1, event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=8))),
            entry(2, event_pb2.Event(harness_exited=event_pb2.HarnessExited(exit_code=0))),
        ]
        ready = await runner.lookup(idempotency_key=str(requested))
        assert not ready.failed
        assert ready.summary.session_id == created.session_id
        await (await peer.attachments.get()).closed.wait()

        peer.setup_state = runner_pb2.SETUP_STATE_FAILED
        setup_failure = await runner.lookup(idempotency_key=str(requested))
        assert setup_failure.failed
        assert not setup_failure.HasField("summary")
        assert peer.attachments.empty()


async def test_open_without_a_client_session_id_mints_one_and_survives_a_lost_response(
    cluster: Cluster, resources: Resources, token_file: Path, peer: Peer, engine: AsyncEngine, sessions: SessionRegistry
) -> None:
    """A client predating client-minted Session IDs names only its Open key."""
    peer.answer_open = False
    request = sandbox_service_pb2.CreateSessionRequest(
        sandbox=DESTINATION,
        idempotency_key="lost-response",
        spec=runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, cwd="/state", model="test-model"),
    )
    request.override_mask.paths.extend(["harness", "cwd", "model"])
    async with service_client(resources, token_file, cluster.provisioning) as remote:
        call = remote.gateway.CreateSession(request, metadata=await remote.gateway_metadata(), timeout=10)
        async with asyncio.timeout(5):
            first = await peer.attachments.get()
        call.cancel()  # reply unknown to caller, reservation and runner attachment already committed
        with pytest.raises((asyncio.CancelledError, grpc.aio.AioRpcError)):
            await call
        await first.closed.wait()
        peer.answer_open = True
        recovered = await remote.gateway.CreateSession(request, metadata=await remote.gateway_metadata(), timeout=5)
        second = await peer.attachments.get()
        assert first.opened == second.opened
        assert recovered.attached.session_id == recovered.session_id
        assert second.opened.session_id == f"r-{recovered.session_id}"
        await second.closed.wait()


async def test_open_refuses_a_session_id_that_is_not_a_uuid(
    resources: Resources, token_file: Path, peer: Peer, engine: AsyncEngine, sessions: SessionRegistry
) -> None:
    request = wire.create_proto(DESTINATION, uuid4(), {"harness": "HARNESS_CODEX", "cwd": "/state", "model": "m"}, None)
    request.session_id = "r-not-a-uuid"
    async with service_client(resources, token_file) as remote:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await remote.gateway.CreateSession(request, metadata=await remote.gateway_metadata(), timeout=5)
    assert refused.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert peer.attachments.empty()


async def test_managed_open_expands_default_cwd_with_reserved_id(
    resources: Resources, token_file: Path, peer: Peer, engine: AsyncEngine, sessions: SessionRegistry, cluster: Cluster
) -> None:
    cluster.set_binding(
        sandbox_service_pb2.SandboxBinding(
            session_defaults=sandbox_service_pb2.SessionDefaults(
                harness=runner_pb2.HARNESS_CODEX, model="test-model", cwd="/state/workspaces/{session_id}"
            )
        )
    )
    async with service_client(resources, token_file, cluster.provisioning) as remote:
        created = await remote.runner(DESTINATION).create(session_id=uuid4(), spec={})
        opened = await peer.attachments.get()
        assert opened.opened.spec.cwd == f"/state/workspaces/{created.session_id}"
        await opened.closed.wait()


async def test_admission_is_exact_native_evidence(remote: SandboxServiceClient, peer: Peer) -> None:
    command = command_pb2.Command(command_id="notice", submit_input=command_pb2.SubmitInput(text="7 messages"))
    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(remote.runner(DESTINATION).command("session", command, after_cursor=4))
        connection = await peer.attachments.get()
        assert connection.opened.follow.after_cursor == 4
        assert not connection.opened.HasField("spec")
        assert not connection.opened.HasField("setup_script")
        assert (await connection.commands.get()).command == command
        assert (await connection.commands.get()).HasField("detach")
        wrong = command_pb2.Command(command_id="notice", submit_input=command_pb2.SubmitInput(text="different"))
        connection.responses.put_nowait(runner_pb2.ServerMessage(event_entry=admission(wrong, 5)))
        assert not pending.done()
        other = command_pb2.Command(command_id="other", submit_input=command.submit_input)
        connection.responses.put_nowait(runner_pb2.ServerMessage(event_entry=admission(other, 6)))
        receipt = admission(command, 7)
        connection.responses.put_nowait(runner_pb2.ServerMessage(event_entry=receipt))
        assert await pending == receipt
        await connection.closed.wait()


async def test_unanswered_runner_open_is_a_deadline_not_a_state_rejection(
    remote: SandboxServiceClient, peer: Peer, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer.answer_open = False
    monkeypatch.setattr("agentplane.runner.client.OBSERVE_ANSWER_S", 0.05)
    async with asyncio.timeout(8):
        with pytest.raises(TimeoutError, match="uncertain"):
            await remote.runner(DESTINATION).command(
                "session",
                command_pb2.Command(command_id="unanswered", submit_input=command_pb2.SubmitInput(text="hi")),
                after_cursor=0,
            )
        connection = await peer.attachments.get()
        await connection.closed.wait()
        assert connection.commands.empty()


async def test_command_uses_runner_ack_budget_not_unary_request_budget(
    resources: Resources, token_file: Path, peer: Peer
) -> None:
    command = command_pb2.Command(command_id="slow-ack", submit_input=command_pb2.SubmitInput(text="hi"))
    # Unary authorization/destination work must not cap the runner's separate receipt wait.
    async with service_client(
        replace(resources, admission_timeout_s=0.1, runner_admission_ack_timeout_s=1), token_file
    ) as remote:
        remote.request_timeout_s = 0.1  # The client also must not use its short generic RPC deadline.
        async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
            submitted = tasks.create_task(remote.runner(DESTINATION).command("session", command, after_cursor=0))
            connection = await peer.attachments.get()
            assert (await connection.commands.get()).command == command
            assert (await connection.commands.get()).HasField("detach")
            await asyncio.sleep(0.2)
            receipt = admission(command, 1)
            connection.responses.put_nowait(runner_pb2.ServerMessage(event_entry=receipt))
            assert await submitted == receipt


async def test_timeout_is_uncertain_and_closes_attachment(remote: SandboxServiceClient, peer: Peer) -> None:
    command = command_pb2.Command(command_id="uncertain", submit_input=command_pb2.SubmitInput(text="hi"))

    async def submit() -> None:
        with pytest.raises(TimeoutError):
            await remote.runner(DESTINATION).command("session", command, after_cursor=0)

    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(submit())
        connection = await peer.attachments.get()
        assert (await connection.commands.get()).command == command
        await pending
        await connection.closed.wait()
        assert peer.attachments.empty()  # The client did not retry the uncertain mutation.


async def test_stopped_command_does_not_send_input(remote: SandboxServiceClient, peer: Peer) -> None:
    peer.state = runner_pb2.HARNESS_STATE_STOPPED
    command = command_pb2.Command(command_id="stopped", submit_input=command_pb2.SubmitInput(text="hi"))
    async with asyncio.timeout(8):
        with pytest.raises(RunnerError):
            await remote.runner(DESTINATION).command("session", command, after_cursor=0)
        connection = await peer.attachments.get()
        await connection.closed.wait()
        assert not connection.opened.HasField("spec")
        assert connection.commands.empty()


@pytest.mark.parametrize(
    "metadata",
    [
        (),
        (("authorization", "Bearer invalid"),),
        (("authorization", f"Bearer {CALLER_TOKEN}"), ("authorization", f"Bearer {CALLER_TOKEN}")),
    ],
)
async def test_missing_invalid_or_duplicate_bearer_is_rejected(
    remote: SandboxServiceClient, peer: Peer, metadata: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as rejected:
        await remote.gateway.ListSessions(
            sandbox_service_pb2.SandboxRequest(destination=DESTINATION), metadata=metadata
        )
    assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert peer.attachments.empty()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"owner": sandbox_service_pb2.ServiceAccount(namespace=SANDBOX_NAMESPACE, name="other")},
            grpc.StatusCode.NOT_FOUND,
        ),
        ({"sandbox_uid": str(uuid4())}, grpc.StatusCode.NOT_FOUND),
    ],
)
async def test_destination_authority_and_incarnation(
    remote: SandboxServiceClient, peer: Peer, change: dict[str, object], code: grpc.StatusCode
) -> None:
    destination = SandboxDestination()
    destination.CopyFrom(DESTINATION)
    if "owner" in change:
        destination.owner.name = "other"
    else:
        destination.sandbox_uid = str(change["sandbox_uid"])
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(destination).list_sessions()
    assert rejected.value.code == code
    assert peer.attachments.empty()


async def test_only_the_sandbox_service_may_seal(remote: SandboxServiceClient, peer: Peer) -> None:
    # An allowed service caller is not the Sandbox Service: sealing a Sandbox is teardown's alone.
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).seal()
    assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert peer.attachments.empty()


@pytest.mark.parametrize("account_name", [ACCOUNT, "unlisted-service"])
async def test_unlisted_callers_rejected_before_lookup(
    resources: Resources, token_file: Path, peer: Peer, cluster: Cluster, account_name: str
) -> None:
    # Owning the destination does not grant service access. The same allowlist gates all RPCs.
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{account_name}",
        pod_name="test-unlisted-pod",
        pod_uid="test-unlisted-uid",
        audiences=(AUDIENCE,),
    )
    configured = replace(
        resources, caller_accounts=frozenset({ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="allowed-service")})
    )
    cluster.fake.objects[SANDBOXES_PLURAL].clear()
    destination = sandbox_service_pb2.SessionDestination(sandbox=DESTINATION, session_id="session")
    async with service_client(configured, token_file, cluster.provisioning) as remote:
        for call, request in (
            (remote.stub.ListSandboxes, Empty()),
            (remote.stub.GetSandbox, sandbox_service_pb2.GetSandboxRequest(name=SANDBOX)),
            (remote.stub.CreateSandbox, sandbox_service_pb2.CreateSandboxRequest()),
            (remote.stub.SuspendSandbox, sandbox_service_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.ResumeSandbox, sandbox_service_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.DeleteSandbox, sandbox_service_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.stub.ListTemplates, Empty()),
            (remote.stub.GrantEgress, sandbox_service_pb2.GrantEgressRequest(destination=DESTINATION)),
            (remote.stub.RevokeEgress, sandbox_service_pb2.RevokeEgressRequest()),
            (remote.gateway.ListSessions, sandbox_service_pb2.SandboxRequest(destination=DESTINATION)),
            (remote.gateway.ResumeSession, sandbox_service_pb2.SessionRequest(destination=destination)),
            (remote.gateway.SubmitCommand, sandbox_service_pb2.SubmitCommandRequest(destination=destination)),
        ):
            with pytest.raises(ServiceError) as rejected:
                await remote.unary(call, request)
            assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(ServiceError) as rejected:
            await remote.runner(DESTINATION).list_sessions()
        assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert cluster.fake.pod_reads == 0
    assert peer.attachments.empty()


@pytest.mark.parametrize("pending_launch", [False, True])
async def test_open_and_resume_refuse_unready_grants_without_app(
    resources: Resources,
    token_file: Path,
    cluster: Cluster,
    peer: Peer,
    sessions: SessionRegistry,
    pending_launch: bool,
) -> None:
    annotations = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["metadata"].setdefault("annotations", {})
    if pending_launch:
        annotations[PROVISIONING_ANNOTATION] = "{}"
    else:
        grant = ResolvedGrant(
            name="config",
            grant=ParseDict(
                RoleBindingGrant(
                    kind="RoleBinding", namespace=SANDBOX_NAMESPACE, role_ref=RoleRef(kind="Role", name="config-reader")
                ).model_dump(mode="json"),
                sandbox_service_pb2.KubernetesGrant(),
            ),
        )
        annotations[KUBERNETES_GRANTS_ANNOTATION] = json.dumps([MessageToDict(grant, preserving_proto_field_name=True)])
        annotations[KUBERNETES_GRANTS_READY_ANNOTATION] = "false"
    view = await cluster.inventory.get(SANDBOX)
    if pending_launch:
        assert view.launch_grants_pending
    else:
        assert not view.kubernetes_grants_ready
    configured = replace(
        resources, sessions=sessions, caller_accounts=frozenset({OWNER}), platform_instructions="Test guidance"
    )
    async with service_client(configured, token_file, cluster.provisioning) as caller:
        runner = caller.runner(DESTINATION)
        with pytest.raises(ServiceError) as opened:
            await runner.create(session_id=uuid4(), spec={"harness": "HARNESS_CODEX", "cwd": "/w", "model": "test"})
        assert opened.value.code == grpc.StatusCode.UNAVAILABLE
        with pytest.raises(ServiceError) as resumed:
            await runner.resume("session")
        assert resumed.value.code == grpc.StatusCode.UNAVAILABLE
    assert peer.attachments.empty()


@pytest.mark.parametrize(
    "command",
    [
        command_pb2.Command(),
        command_pb2.Command(command_id="missing-operation"),
        command_pb2.Command(submit_input=command_pb2.SubmitInput(text="missing-id")),
    ],
)
async def test_invalid_command_is_not_submitted(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster, command: command_pb2.Command
) -> None:
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).command("session", command, after_cursor=0)
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert cluster.fake.pod_reads == 0
    assert peer.attachments.empty()


async def test_command_cancellation_closes_native_attachment(remote: SandboxServiceClient, peer: Peer) -> None:
    command = command_pb2.Command(command_id="cancel", submit_input=command_pb2.SubmitInput(text="notice"))
    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(remote.runner(DESTINATION).command("session", command, after_cursor=0))
        connection = await peer.attachments.get()
        assert (await connection.commands.get()).command == command
        assert (await connection.commands.get()).HasField("detach")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await connection.closed.wait()


@pytest.mark.parametrize("error", [None, "test journal failure"])
async def test_native_eof_or_error_is_not_admission(
    remote: SandboxServiceClient, peer: Peer, error: str | None
) -> None:
    command = command_pb2.Command(command_id="end", submit_input=command_pb2.SubmitInput(text="notice"))

    async def submit() -> None:
        with pytest.raises(RunnerError):
            await remote.runner(DESTINATION).command("session", command, after_cursor=0)

    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(submit())
        connection = await peer.attachments.get()
        assert (await connection.commands.get()).command == command
        connection.responses.put_nowait(runner_pb2.ServerMessage(error=error) if error else None)
        await pending
        await connection.closed.wait()
        assert peer.attachments.empty()


@pytest.mark.parametrize("field", ["controller", "uid", "name", "kind", "apiVersion"])
async def test_unverified_pod_owner_is_unavailable(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster, field: str
) -> None:
    cluster.fake.pods[SANDBOX]["metadata"]["ownerReferences"][0][field] = (
        False if field == "controller" else "test-foreign"
    )
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).list_sessions()
    assert rejected.value.code == grpc.StatusCode.UNAVAILABLE
    assert peer.attachments.empty()


@pytest.mark.parametrize("condition", ["different-account", "suspended", "deleting"])
async def test_unavailable_destination_does_not_contact_runner(
    remote: SandboxServiceClient, peer: Peer, cluster: Cluster, condition: str
) -> None:
    stored = cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]
    match condition:
        case "different-account":
            cluster.fake.pods[SANDBOX]["spec"]["serviceAccountName"] = "test-other"
        case "suspended":
            stored["spec"]["operatingMode"] = "Suspended"
        case "deleting":
            stored["metadata"]["deletionTimestamp"] = "2026-09-01T12:00:00Z"
    with pytest.raises(ServiceError) as rejected:
        await remote.runner(DESTINATION).list_sessions()
    assert rejected.value.code == grpc.StatusCode.UNAVAILABLE
    assert peer.attachments.empty()


if __name__ == "__main__":
    pytest_bazel.main()
