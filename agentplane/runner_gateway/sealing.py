"""Teardown's seal, run by the gateway replica holding the Sandbox's runner connection (or, for a runner
reached through its inbound API, any gateway replica): fence and seal every Session, and name each
one's seal as retention holders name the Session."""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

import grpc

from agentplane.proto import runner_control_pb2 as control_pb2, runner_pb2
from agentplane.proto.sandbox_service_pb2 import Sandbox, SandboxDestination
from agentplane.runner.client import RunnerClient
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, LifecycleControl
from agentplane.runner_gateway.runner_control import ControlOperation
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.sandbox_service.destinations import DestinationResolver

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Stopping harnesses can take a while, and the Sandbox Service reconciles one Sandbox at a time. Seal is
# idempotent, so a reconcile that runs out of time seals again on its retry.
SEAL_S = 120


@dataclass(frozen=True)
class Sealing:
    destinations: DestinationResolver
    sessions: SessionRegistry
    connections: ConnectionStore
    control: LifecycleControl
    runner_grpc_channel_options: dict[str, int | str]

    async def seal(self, sandbox: Sandbox) -> runner_pb2.SealResponse:
        """The runner's seal, each Session named as holders name it: the public Session ID, or the
        runner's own ID for a Session opened with a caller-chosen one."""
        sandbox_uid = UUID(sandbox.uid)
        if ControlOperation.LIFECYCLE in await self.connections.read_operations(sandbox_uid):
            # The caller resolves the control Service again and retries until it reaches the holder.
            if self.control.holder(sandbox_uid) is None:
                raise ConnectionNotHeldError(f"Sandbox {sandbox.name}'s runner connection is not held here")
            async with asyncio.timeout(SEAL_S):
                result = await self.control.request(
                    sandbox_uid, control_pb2.ControlRequest(seal=runner_pb2.SealRequest())
                )
            sealed = result.seal
        else:
            destination = SandboxDestination(
                owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid
            )
            endpoint = await self.destinations.resolve(destination, writes=False)
            client = RunnerClient(endpoint.target, channel_options=self.runner_grpc_channel_options)
            try:
                async with asyncio.timeout(SEAL_S):
                    sealed = await self._seal_sessions(client)
            finally:
                await client.close()
        for session in sealed.sessions:
            public_id = await self.sessions.session_id(
                sandbox_namespace=sandbox.namespace,
                sandbox_name=sandbox.name,
                sandbox_uid=sandbox_uid,
                runner_session_id=session.session_id,
            )
            if public_id is not None:
                session.session_id = str(public_id)
        return sealed

    @staticmethod
    async def _seal_sessions(client: RunnerClient) -> runner_pb2.SealResponse:
        try:
            return await client.seal()
        except grpc.aio.AioRpcError as error:
            if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
        return await Sealing._stop_legacy(client)

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
