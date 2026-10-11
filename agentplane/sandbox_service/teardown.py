"""DeleteSandbox's teardown, driven by the controller: seal every Session, then delete.

`SandboxInventory.begin_teardown` runs the suspended Sandbox again. Here its runner seals every
Session; the seal cursors go into the retention holds and the Sandbox is suspended again; it is
deleted once every hold is confirmed through its seal. While the runner is unreachable, the Sandbox
stays in `TEARDOWN_PHASE_SEALING` for the operator: nothing deletes an unsealed Sandbox. A Sandbox
whose runner connection agreed `lifecycle` is sealed over that connection, by the replica holding it.

A runner that pushes its Events to an ingress reports each seal flushed once that ingress has
acknowledged it. Its Sandbox needs no holds: it is deleted straight from `TEARDOWN_PHASE_SEALING` once
every seal is flushed, and sealed again every `FLUSH_RETRY_S` until then.
"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

import grpc

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.proto import runner_channel_pb2 as channel_pb2, runner_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner_gateway.channel_control import ChannelControl, ConnectionNotHeldError
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.runner_channel import ChannelOperation
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.protocol_pb2 import Sandbox, SandboxDestination, TeardownPhase

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Stopping harnesses can take a while, and the controller reconciles one Sandbox at a time. Seal is
# idempotent, so a reconcile that runs out of time seals again on its retry.
SEAL_S = 120
FLUSH_RETRY_S = 10


class SealNotFlushedError(Exception):
    """The runner sealed every Session, but its ingress has not acknowledged all of the seals yet."""


@dataclass(frozen=True)
class Teardown:
    destinations: DestinationResolver
    sessions: SessionRegistry
    connections: ConnectionStore
    control: ChannelControl
    runner_grpc_channel_options: dict[str, int | str]

    async def advance(self, sandbox: Sandbox) -> None:
        inventory = self.destinations.inventory
        match sandbox.teardown:
            case TeardownPhase.TEARDOWN_PHASE_SEALING:
                seal = await self._seal(sandbox)
                if not seal.pushes:
                    # CLEANUP(added 2026-10-11): FOLLOW_PATH_RETIRE deletes this branch, the holds and
                    #   SealResponse.pushes once every runner pushes (agentplane/plans/task_dag.md).
                    await inventory.record_seal(
                        sandbox.name, uid=sandbox.uid, cursors=await self._cursors(sandbox, seal)
                    )
                    return
                unflushed = [s.session_id for s in seal.sessions if not s.flushed]
                if unflushed:
                    raise SealNotFlushedError(f"{sandbox.name}: ingress has not acknowledged {unflushed=}")
                await inventory.delete_flushed(sandbox.name, uid=sandbox.uid)
            case TeardownPhase.TEARDOWN_PHASE_SEALED:
                # Otherwise a hold change reconciles it again.
                await inventory.delete_if_released(sandbox.name, uid=sandbox.uid)

    async def _seal(self, sandbox: Sandbox) -> runner_pb2.SealResponse:
        sandbox_uid = UUID(sandbox.uid)
        if ChannelOperation.LIFECYCLE in await self.connections.read_operations(sandbox_uid):
            # Every replica reconciles the Sandbox; the others retry until the holder has sealed it.
            if self.control.holder(sandbox_uid) is None:
                raise ConnectionNotHeldError(f"Sandbox {sandbox.name}'s runner connection is not held here")
            async with asyncio.timeout(SEAL_S):
                result = await self.control.request(
                    sandbox_uid, channel_pb2.ControlRequest(seal=runner_pb2.SealRequest())
                )
            return result.seal
        destination = SandboxDestination(owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid)
        endpoint = await self.destinations.resolve(destination, writes=False)
        client = RunnerClient(
            grpc.aio.insecure_channel(
                endpoint.target, options=grpc_channel_option_kvps(self.runner_grpc_channel_options)
            )
        )
        try:
            async with asyncio.timeout(SEAL_S):
                return await self._seal_sessions(client)
        finally:
            await client.close()

    async def _cursors(self, sandbox: Sandbox, seal: runner_pb2.SealResponse) -> dict[str, int]:
        """Each Session's final cursor, keyed as holders name it: the public Session ID, or the
        runner's own ID for a Session opened with a caller-chosen one."""
        cursors = {}
        for sealed in seal.sessions:
            public_id = await self.sessions.session_id(
                sandbox_namespace=sandbox.namespace,
                sandbox_name=sandbox.name,
                sandbox_uid=UUID(sandbox.uid),
                runner_session_id=sealed.session_id,
            )
            cursors[sealed.session_id if public_id is None else str(public_id)] = sealed.cursor
        return cursors

    @staticmethod
    async def _seal_sessions(client: RunnerClient) -> runner_pb2.SealResponse:
        try:
            return await client.seal()
        except grpc.aio.AioRpcError as error:
            if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
        return await Teardown._stop_legacy(client)

    @staticmethod
    async def _stop_legacy(client: RunnerClient) -> runner_pb2.SealResponse:
        """A runner image without Seal: stop every harness, then take each journal's head as its
        final cursor. Only this service's fence keeps new Sessions and commands out."""
        for summary in await client.list_sessions():
            if summary.harness_state != runner_pb2.HARNESS_STATE_RUNNING:
                continue
            logger.info("stopping %s for teardown; its runner cannot seal", summary.session_id)
            attachment = await client.attach(summary.session_id, after_cursor=summary.last_cursor)
            try:
                await attachment.stop_runner_session(f"teardown-{uuid4()}")
                await attachment.drain_until_end()
            finally:
                attachment.cancel()
        return runner_pb2.SealResponse(
            sessions=[
                runner_pb2.SessionSeal(session_id=summary.session_id, cursor=summary.last_cursor)
                for summary in await client.list_sessions()
            ]
        )
