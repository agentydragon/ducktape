"""The app's route to a Sandbox's runner; every runner operation goes through the Runner Gateway."""

from uuid import UUID

from agentplane.app.live import LiveIndex
from agentplane.app.sandbox_models import sandbox_has_ready_pod
from agentplane.app.threads.events.event_log import SessionNotRegisteredError
from agentplane.proto.sandbox_service_pb2 import SandboxDestination, ServiceAccount
from agentplane.runner_gateway.client import Runner, RunnerGatewayClient
from agentplane.sandbox_service.errors import SandboxNotFoundError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//types_protobuf
# The gateway's message stubs, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep //agentplane/proto:runner_gateway_pb2


class SandboxNotReachableError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name=} has no ready, authorized Pod")
        self.name = name


class SandboxSessions:
    def __init__(self, index: LiveIndex, gateway: RunnerGatewayClient) -> None:
        self._index = index
        self.gateway = gateway
        self._clients: dict[str, Runner] = {}

    async def sandbox_of(self, thread_id: UUID) -> str:
        """The Sandbox the Runner Gateway routes the Thread's Session to."""
        found = await self.gateway.session_sandboxes({str(thread_id)})
        if not found:
            raise SessionNotRegisteredError(thread_id)
        return found[0].sandbox

    def client(self, sandbox: str) -> Runner:
        view = self._index.sandbox_view(sandbox)
        if view is None:
            raise SandboxNotFoundError(sandbox)
        if not sandbox_has_ready_pod(view):
            raise SandboxNotReachableError(sandbox)
        destination = SandboxDestination(
            owner=ServiceAccount(namespace=view.service_account.namespace, name=view.service_account.name),
            sandbox=view.name,
            sandbox_uid=str(view.uid),
        )
        if str(view.uid) not in self._clients:
            self._clients[str(view.uid)] = self.gateway.runner(destination)
        return self._clients[str(view.uid)]
