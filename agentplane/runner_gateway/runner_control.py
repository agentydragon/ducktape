"""The runner-initiated control listener: authentication, ownership, liveness, and the frames that
carry the gateway's calls to the runner's `Runner` service.

Contract: `agentplane/docs/runner_control.md`.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import override
from uuid import UUID

import grpc
from kubernetes_asyncio import client as k8s_client

from agentplane import sandbox_resource
from agentplane.proto import grpc_tunnel_pb2, runner_control_pb2, runner_control_pb2_grpc
from agentplane.runner_gateway.connection_records import ConnectionRecords, ConnectionSupersededError, RecordOwner
from agentplane.runner_gateway.lifecycle_control import LifecycleControl, RunnerConnection
from agentplane.sandbox_service.errors import SandboxNotFoundError
from agentplane.sandbox_service.reader import SandboxReader
from agentplane.workload_auth.grpc_caller import resolve_caller
from agentplane.workload_auth.principal import (
    WorkloadPrincipal,
    WorkloadPrincipalRejectedError,
    WorkloadPrincipalResolver,
)
from util.agent_sandbox import SANDBOX_API, SANDBOX_KIND
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControlPeer:
    """The Sandbox incarnation a connection authenticated as; nothing the runner claims."""

    sandbox_name: str
    sandbox_uid: UUID
    sandbox_api_version: str
    pod_uid: str

    @property
    def record_owner(self) -> RecordOwner:
        return RecordOwner(name=self.sandbox_name, uid=self.sandbox_uid, api_version=self.sandbox_api_version)


@dataclass(frozen=True)
class ControlTiming:
    heartbeat_s: float = 10
    liveness_s: float = 30
    touch_s: float = 15
    max_age_s: float = 3600


class PeerRefusedError(Exception):
    """The workload is authentic but not a Sandbox Pod of this inventory."""


class _StreamClosedError(Exception):
    def __init__(self, code: grpc.StatusCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


async def resolve_peer(
    principal: WorkloadPrincipal,
    *,
    sandboxes: SandboxReader,
    custom_objects: CustomObjectsClient,
    core: k8s_client.CoreV1Api,
) -> ControlPeer:
    """Follow the token's Pod to the Sandbox controlling it, the reverse of destination resolution.
    The Sandbox is agent-sandbox's or Agentplane's own."""
    if principal.namespace != sandboxes.namespace:
        raise PeerRefusedError("workload is outside the Sandbox namespace")
    try:
        pod = await core.read_namespaced_pod(principal.pod_name, sandboxes.namespace)
    except k8s_client.ApiException as error:
        if error.status == 404:
            raise WorkloadPrincipalRejectedError("bearer Pod no longer exists") from error
        raise
    metadata = pod.metadata
    if metadata is None or metadata.uid != principal.pod_uid or metadata.deletion_timestamp is not None:
        raise WorkloadPrincipalRejectedError("bearer Pod was replaced or is being deleted")
    controllers = [ref for ref in metadata.owner_references or [] if ref.controller]
    if len(controllers) != 1 or controllers[0].kind != SANDBOX_KIND:
        raise PeerRefusedError("Pod is not controlled by a Sandbox")
    owner = controllers[0]
    match owner.api_version:
        case SANDBOX_API.api_version:
            try:
                view = await sandboxes.get(owner.name)
            except SandboxNotFoundError as error:
                raise PeerRefusedError("Pod's Sandbox is not in this inventory") from error
            uid, account = view.uid, view.service_account.name
        case sandbox_resource.SANDBOX_API_VERSION:
            own = await sandbox_resource.read_sandbox(custom_objects, sandboxes.namespace, owner.name)
            if own is None:
                raise PeerRefusedError("Pod's Sandbox does not exist")
            # The Sandbox Controller runs each Sandbox's Pods as the ServiceAccount of its name.
            uid, account = str(own.metadata.uid), own.metadata.name
        case _:
            raise PeerRefusedError("Pod is not controlled by a Sandbox")
    if uid != owner.uid:
        raise PeerRefusedError("Pod belongs to another incarnation of its Sandbox")
    if account != principal.service_account_name:
        raise PeerRefusedError("Sandbox runs as a different ServiceAccount")
    return ControlPeer(
        sandbox_name=owner.name, sandbox_uid=UUID(uid), sandbox_api_version=owner.api_version, pod_uid=principal.pod_uid
    )


class RunnerControlService(runner_control_pb2_grpc.RunnerControlServicer):
    def __init__(
        self,
        *,
        principals: WorkloadPrincipalResolver,
        sandboxes: SandboxReader,
        custom_objects: CustomObjectsClient,
        core: k8s_client.CoreV1Api,
        records: ConnectionRecords,
        control: LifecycleControl,
        timing: ControlTiming,
    ) -> None:
        self.principals = principals
        self.sandboxes = sandboxes
        self.custom_objects = custom_objects
        self.core = core
        self.records = records
        self.control = control
        self.timing = timing

    async def authenticate(self, context: grpc.aio.ServicerContext) -> tuple[WorkloadPrincipal, ControlPeer]:
        principal = await resolve_caller(self.principals, context)
        return principal, await resolve_peer(
            principal, sandboxes=self.sandboxes, custom_objects=self.custom_objects, core=self.core
        )

    # mypy-protobuf types the handler as returning an iterator; explicit writes keep every send
    # inside the liveness bound.
    @override
    async def Connect(  # type: ignore[override]
        self, request_iterator: AsyncIterator[runner_control_pb2.RunnerFrame], context: grpc.aio.ServicerContext
    ) -> None:
        try:
            async with asyncio.timeout(self.timing.liveness_s):
                principal, peer = await self.authenticate(context)
            try:
                async with asyncio.timeout(self.timing.liveness_s):
                    hello = await anext(request_iterator)
            except StopAsyncIteration, TimeoutError:
                hello = runner_control_pb2.RunnerFrame()
            if hello.WhichOneof("frame") != "hello" or not hello.hello.runner_boot_id:
                raise _StreamClosedError(grpc.StatusCode.INVALID_ARGUMENT, "the first frame must be Hello")
            epoch = await self.records.accept(
                peer.record_owner, pod_uid=peer.pod_uid, runner_boot_id=hello.hello.runner_boot_id
            )
            async with asyncio.timeout(self.timing.liveness_s):
                await context.write(
                    runner_control_pb2.ServiceFrame(welcome=runner_control_pb2.Welcome(connection_epoch=epoch))
                )
            await self.serve(principal, peer, epoch, request_iterator, context)
        except WorkloadPrincipalRejectedError:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
        except PeerRefusedError:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "not a Sandbox Pod")
        except _StreamClosedError as closed:
            await context.abort(closed.code, closed.detail)
        except TimeoutError:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "control stream operation timed out")
        except k8s_client.ApiException:
            logger.exception("Runner control stream backend failure")
            await context.abort(grpc.StatusCode.UNAVAILABLE, "control backend unavailable")

    async def serve(
        self,
        principal: WorkloadPrincipal,
        peer: ControlPeer,
        epoch: int,
        frames: AsyncIterator[runner_control_pb2.RunnerFrame],
        context: grpc.aio.ServicerContext,
    ) -> None:
        """Keep the connection until the runner leaves, goes quiet, ages out or is superseded."""
        loop = asyncio.get_running_loop()
        started = last_touch = last_frame = loop.time()
        last_frame_at = datetime.now(UTC)
        writing = asyncio.Lock()

        async def send(frame: runner_control_pb2.ServiceFrame) -> None:
            async with writing, asyncio.timeout(self.timing.liveness_s):
                await context.write(frame)

        async def call(tunneled: grpc_tunnel_pb2.Call) -> None:
            await send(runner_control_pb2.ServiceFrame(call=tunneled))

        async def read(connection: RunnerConnection) -> None:
            nonlocal last_frame, last_frame_at
            async for frame in frames:
                last_frame, last_frame_at = loop.time(), datetime.now(UTC)
                match frame.WhichOneof("frame"):
                    case "heartbeat":
                        pass
                    case "reply":
                        connection.tunnel.answer(frame.reply)
                    case kind:
                        raise _StreamClosedError(grpc.StatusCode.INVALID_ARGUMENT, f"unexpected {kind} frame")

        async def keep_alive() -> None:
            nonlocal last_touch
            while True:
                await asyncio.sleep(self.timing.heartbeat_s)
                now = loop.time()
                if now - last_frame > self.timing.liveness_s:
                    raise _StreamClosedError(grpc.StatusCode.UNAVAILABLE, "no frame from the runner")
                if now - started > self.timing.max_age_s:
                    raise _StreamClosedError(grpc.StatusCode.UNAVAILABLE, "maximum connection age reached")
                if now - last_touch >= self.timing.touch_s:
                    try:
                        await self.records.renew(peer.record_owner, epoch=epoch, seen_at=last_frame_at)
                    except ConnectionSupersededError as error:
                        raise _StreamClosedError(grpc.StatusCode.ABORTED, "superseded by a newer connection") from error
                    # Revocation: the Pod or Sandbox behind this connection may have changed.
                    if (
                        await resolve_peer(
                            principal, sandboxes=self.sandboxes, custom_objects=self.custom_objects, core=self.core
                        )
                        != peer
                    ):
                        raise _StreamClosedError(grpc.StatusCode.PERMISSION_DENIED, "the Sandbox's routing changed")
                    last_touch = now
                await send(runner_control_pb2.ServiceFrame(heartbeat=runner_control_pb2.Heartbeat()))

        async with self.control.connect(peer.sandbox_uid, epoch=epoch, send=call) as connection:
            tasks = [asyncio.create_task(read(connection)), asyncio.create_task(keep_alive())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        # Only the reader returns, when the runner closed its side cleanly.
        for task in done:
            task.result()
