"""The runner-initiated channel listener: authentication, ownership, liveness and command delivery.

Contract: `agentplane/docs/runner_channel.md`.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import override
from uuid import UUID

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.runner_channel import protocol_pb2, protocol_pb2_grpc
from agentplane.sandbox_service.channel_delivery import ChannelDelivery
from agentplane.sandbox_service.commands.connections import ConnectionStore, ConnectionSupersededError
from agentplane.sandbox_service.commands.dispatch import CommandKey, ReceiptMismatchError
from agentplane.sandbox_service.commands.store import SubmissionConflictError
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.workload_auth.bearer import parse_bearer, sole_header
from agentplane.workload_auth.principal import (
    WorkloadPrincipal,
    WorkloadPrincipalRejectedError,
    WorkloadPrincipalResolver,
)
from util.agent_sandbox import SANDBOX_API, SANDBOX_KIND

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Attempts sent on one stream without a receipt yet; past this, a claim waits for re-notification.
MAX_UNRECEIPTED = 64


class ChannelOperation(StrEnum):
    COMMANDS = "commands"


@dataclass(frozen=True)
class ChannelPeer:
    """The Sandbox incarnation a connection authenticated as; nothing the runner claims."""

    sandbox_name: str
    sandbox_uid: UUID
    pod_uid: str


@dataclass(frozen=True)
class ChannelTiming:
    heartbeat_s: float = 10
    liveness_s: float = 30
    touch_s: float = 15
    max_age_s: float = 3600


class ChannelRefusedError(Exception):
    """The workload is authentic but not a Sandbox Pod of this inventory."""


class _ChannelClosedError(Exception):
    def __init__(self, code: grpc.StatusCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


async def resolve_peer(
    principal: WorkloadPrincipal, *, inventory: SandboxInventory, core: k8s_client.CoreV1Api
) -> ChannelPeer:
    """Follow the token's Pod to the Sandbox controlling it, the reverse of destination resolution."""
    if principal.namespace != inventory.namespace:
        raise ChannelRefusedError("workload is outside the Sandbox namespace")
    try:
        pod = await core.read_namespaced_pod(principal.pod_name, inventory.namespace)
    except k8s_client.ApiException as error:
        if error.status == 404:
            raise WorkloadPrincipalRejectedError("bearer Pod no longer exists") from error
        raise
    metadata = pod.metadata
    if metadata is None or metadata.uid != principal.pod_uid or metadata.deletion_timestamp is not None:
        raise WorkloadPrincipalRejectedError("bearer Pod was replaced or is being deleted")
    controllers = [ref for ref in metadata.owner_references or [] if ref.controller]
    if (
        len(controllers) != 1
        or controllers[0].api_version != SANDBOX_API.api_version
        or controllers[0].kind != SANDBOX_KIND
    ):
        raise ChannelRefusedError("Pod is not controlled by a Sandbox")
    owner = controllers[0]
    try:
        sandbox = await inventory.get(owner.name)
    except SandboxNotFoundError as error:
        raise ChannelRefusedError("Pod's Sandbox is not in this inventory") from error
    if sandbox.uid != owner.uid:
        raise ChannelRefusedError("Pod belongs to another incarnation of its Sandbox")
    if sandbox.service_account.name != principal.service_account_name:
        raise ChannelRefusedError("Sandbox runs as a different ServiceAccount")
    return ChannelPeer(sandbox_name=sandbox.name, sandbox_uid=UUID(sandbox.uid), pod_uid=principal.pod_uid)


class RunnerChannelService(protocol_pb2_grpc.RunnerChannelServicer):
    def __init__(
        self,
        *,
        principals: WorkloadPrincipalResolver,
        inventory: SandboxInventory,
        core: k8s_client.CoreV1Api,
        connections: ConnectionStore,
        delivery: ChannelDelivery,
        replica: str,
        timing: ChannelTiming,
    ) -> None:
        self.principals = principals
        self.inventory = inventory
        self.core = core
        self.connections = connections
        self.delivery = delivery
        self.replica = replica
        self.timing = timing

    async def authenticate(self, context: grpc.aio.ServicerContext) -> tuple[WorkloadPrincipal, ChannelPeer]:
        values = [value for key, value in (context.invocation_metadata() or ()) if key == "authorization"]
        if any(not isinstance(value, str) for value in values):
            raise WorkloadPrincipalRejectedError("invalid bearer metadata")
        value = sole_header([value for value in values if isinstance(value, str)])
        token = parse_bearer(value) if value is not None else None
        if token is None:
            raise WorkloadPrincipalRejectedError("invalid bearer metadata")
        principal = await self.principals.resolve_workload(token)
        return principal, await resolve_peer(principal, inventory=self.inventory, core=self.core)

    # mypy-protobuf types the handler as returning an iterator; explicit writes keep every send
    # inside the liveness bound.
    @override
    async def Connect(  # type: ignore[override]
        self, request_iterator: AsyncIterator[protocol_pb2.RunnerFrame], context: grpc.aio.ServicerContext
    ) -> None:
        try:
            async with asyncio.timeout(self.timing.liveness_s):
                principal, peer = await self.authenticate(context)
            try:
                async with asyncio.timeout(self.timing.liveness_s):
                    hello = await anext(request_iterator)
            except StopAsyncIteration, TimeoutError:
                hello = protocol_pb2.RunnerFrame()
            if hello.WhichOneof("frame") != "hello" or not hello.hello.runner_boot_id:
                raise _ChannelClosedError(grpc.StatusCode.INVALID_ARGUMENT, "the first frame must be Hello")
            capabilities = set(hello.hello.capabilities) & set(ChannelOperation)
            epoch = await self.connections.accept(
                sandbox_uid=peer.sandbox_uid,
                pod_uid=peer.pod_uid,
                owner_replica=self.replica,
                runner_boot_id=hello.hello.runner_boot_id,
                operations=capabilities,
            )
            async with asyncio.timeout(self.timing.liveness_s):
                await context.write(
                    protocol_pb2.ServiceFrame(
                        welcome=protocol_pb2.Welcome(connection_epoch=epoch, capabilities=sorted(capabilities))
                    )
                )
            with self.delivery.route(peer.sandbox_uid) as attempts:
                await self.serve(principal, peer, epoch, capabilities, attempts, request_iterator, context)
        except WorkloadPrincipalRejectedError:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
        except ChannelRefusedError:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "not a Sandbox Pod")
        except _ChannelClosedError as closed:
            await context.abort(closed.code, closed.detail)
        except TimeoutError:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "channel operation timed out")
        except k8s_client.ApiException, SQLAlchemyError:
            logger.exception("Runner channel backend failure")
            await context.abort(grpc.StatusCode.UNAVAILABLE, "channel backend unavailable")

    async def serve(
        self,
        principal: WorkloadPrincipal,
        peer: ChannelPeer,
        epoch: int,
        capabilities: set[str],
        attempts: asyncio.Queue[CommandKey],
        frames: AsyncIterator[protocol_pb2.RunnerFrame],
        context: grpc.aio.ServicerContext,
    ) -> None:
        """Keep the connection until the runner leaves, goes quiet, ages out or is superseded."""
        loop = asyncio.get_running_loop()
        started = last_touch = last_frame = loop.time()
        last_frame_at = datetime.now(UTC)
        writing = asyncio.Lock()
        unreceipted: dict[tuple[str, str], datetime] = {}  # (runner session, command ID) -> deadline
        commands = ChannelOperation.COMMANDS in capabilities

        async def send(frame: protocol_pb2.ServiceFrame) -> None:
            async with writing, asyncio.timeout(self.timing.liveness_s):
                await context.write(frame)

        async def read() -> None:
            nonlocal last_frame, last_frame_at
            async for frame in frames:
                last_frame, last_frame_at = loop.time(), datetime.now(UTC)
                match frame.WhichOneof("frame"):
                    case "heartbeat":
                        pass
                    case "receipt" if commands:
                        try:
                            await self.delivery.dispatches.record(peer.sandbox_uid, frame.receipt)
                        except (ReceiptMismatchError, SubmissionConflictError) as error:
                            raise _ChannelClosedError(grpc.StatusCode.INVALID_ARGUMENT, str(error)) from error
                        unreceipted.pop((frame.receipt.session_id, frame.receipt.command_id), None)
                    case kind:
                        raise _ChannelClosedError(grpc.StatusCode.INVALID_ARGUMENT, f"unexpected {kind} frame")

        async def deliver() -> None:
            while True:
                key = await attempts.get()
                if not commands:
                    continue
                now = datetime.now(UTC)
                for expired in [attempt for attempt, deadline in unreceipted.items() if deadline <= now]:
                    del unreceipted[expired]
                if len(unreceipted) >= MAX_UNRECEIPTED:
                    continue
                delivery = await self.delivery.dispatches.claim(key, sandbox_uid=peer.sandbox_uid, epoch=epoch)
                if delivery is None:
                    continue
                unreceipted[delivery.runner_session_id, key.command_id] = delivery.deadline
                await send(
                    protocol_pb2.ServiceFrame(
                        deliver=protocol_pb2.DeliverCommand(
                            session_id=delivery.runner_session_id, command=delivery.command
                        )
                    )
                )

        async def keep_alive() -> None:
            nonlocal last_touch
            while True:
                await asyncio.sleep(self.timing.heartbeat_s)
                now = loop.time()
                if now - last_frame > self.timing.liveness_s:
                    raise _ChannelClosedError(grpc.StatusCode.UNAVAILABLE, "no frame from the runner")
                if now - started > self.timing.max_age_s:
                    raise _ChannelClosedError(grpc.StatusCode.UNAVAILABLE, "maximum connection age reached")
                if now - last_touch >= self.timing.touch_s:
                    try:
                        await self.connections.touch(sandbox_uid=peer.sandbox_uid, epoch=epoch, seen_at=last_frame_at)
                    except ConnectionSupersededError as error:
                        raise _ChannelClosedError(
                            grpc.StatusCode.ABORTED, "superseded by a newer connection"
                        ) from error
                    # Revocation: the Pod or Sandbox behind this connection may have changed.
                    if await resolve_peer(principal, inventory=self.inventory, core=self.core) != peer:
                        raise _ChannelClosedError(grpc.StatusCode.PERMISSION_DENIED, "the Sandbox's routing changed")
                    last_touch = now
                await send(protocol_pb2.ServiceFrame(heartbeat=protocol_pb2.Heartbeat()))

        tasks = [asyncio.create_task(task()) for task in (read, deliver, keep_alive)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Only the reader returns, when the runner closed its side cleanly.
        for task in done:
            task.result()
