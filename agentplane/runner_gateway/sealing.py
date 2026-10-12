"""Teardown's seal, run by the gateway replica holding the Sandbox's runner connection: fence and seal
every Session, and name each one's seal by its public Session ID."""

import asyncio
from dataclasses import dataclass
from uuid import UUID

from agentplane.proto import runner_control_pb2, runner_pb2
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, LifecycleControl
from agentplane.runner_gateway.session_registry import SessionRegistry

# gazelle:include_dep @pypi//protobuf

# Stopping harnesses can take a while, and the Sandbox Service reconciles one Sandbox at a time. Seal is
# idempotent, so a reconcile that runs out of time seals again on its retry.
SEAL_S = 120


@dataclass(frozen=True)
class Sealing:
    sessions: SessionRegistry
    control: LifecycleControl

    async def seal(self, *, namespace: str, name: str, sandbox_uid: UUID) -> runner_pb2.SealResponse:
        """The runner's seal; every Session it seals must be registered in this Sandbox incarnation."""
        # The caller resolves the control Service again and retries until it reaches the holder.
        if self.control.holder(sandbox_uid) is None:
            raise ConnectionNotHeldError(f"Sandbox {name}'s runner connection is not held here")
        async with asyncio.timeout(SEAL_S):
            result = await self.control.request(
                sandbox_uid, runner_control_pb2.ControlRequest(seal=runner_pb2.SealRequest())
            )
        sealed = result.seal
        for session in sealed.sessions:
            if (
                await self.sessions.public_id(
                    session.session_id, sandbox_namespace=namespace, sandbox_name=name, sandbox_uid=sandbox_uid
                )
                is None
            ):
                raise ValueError(f"runner Session {session.session_id} has no registered Session")
        return sealed
