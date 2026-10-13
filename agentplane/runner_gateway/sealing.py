"""Teardown's seal, run by the gateway replica holding the Sandbox's runner connection: fence and seal
every Session, and name each one's seal by its public Session ID."""

from dataclasses import dataclass
from uuid import UUID

from agentplane.proto import runner_pb2
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.session_registry import SessionRegistry

# The `Runner` stub types its calls; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_control_pb2
# gazelle:include_dep //agentplane/proto:runner_control_pb2_grpc
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf

# Stopping harnesses can take a while. Seal is idempotent, so a deletion seal that runs out of time seals
# again on its retry.
SEAL_S = 120


@dataclass(frozen=True)
class Sealing:
    sessions: SessionRegistry
    control: LifecycleControl

    async def seal(self, *, namespace: str, name: str, sandbox_uid: UUID) -> runner_pb2.SealResponse:
        """The runner's seal; every Session it seals must be registered in this Sandbox incarnation."""
        # Without the connection the caller resolves the control Service again and retries until it
        # reaches the holder.
        sealed = await self.control.runner(sandbox_uid).Seal(runner_pb2.SealRequest(), timeout=SEAL_S)
        for session in sealed.sessions:
            if (
                await self.sessions.public_id(
                    session.session_id, sandbox_namespace=namespace, sandbox_name=name, sandbox_uid=sandbox_uid
                )
                is None
            ):
                raise ValueError(f"runner Session {session.session_id} has no registered Session")
        return sealed
