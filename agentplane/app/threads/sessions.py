"""App-side session discovery; every runner operation goes through Sandbox Service."""

from agentplane.app.changes import Changes
from agentplane.app.live import LiveIndex
from agentplane.app.sandbox_models import SandboxKind
from agentplane.sandbox_service.client import Runner, SandboxServiceClient
from agentplane.sandbox_service.models import ProvisioningState, SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination, ServiceAccount


class SandboxNotReachableError(Exception):
    def __init__(self, name: str, state: ProvisioningState) -> None:
        super().__init__(f"sandbox {name=} has no reachable runner: it is {state}")
        self.name = name


class SandboxSessions:
    def __init__(self, index: LiveIndex, service: SandboxServiceClient) -> None:
        self._index = index
        self._service = service
        self._clients: dict[str, Runner] = {}

    @property
    def changes(self) -> Changes:
        return self._index.changes

    def running(self) -> set[tuple[SandboxKind, str]]:
        return {
            (view.kind, view.name) for view in self._index.sandbox_views() if view.state is ProvisioningState.RUNNING
        }

    def client(self, sandbox: str, kind: SandboxKind = "agent_sandbox") -> Runner:
        view = self._index.sandbox_view(sandbox, kind)
        if view is None:
            raise SandboxNotFoundError(f"{kind}/{sandbox}")
        if view.state is not ProvisioningState.RUNNING:
            raise SandboxNotReachableError(sandbox, view.state)
        destination = SandboxDestination(
            owner=ServiceAccount(namespace=view.service_account.namespace, name=view.service_account.name),
            sandbox=view.name,
            sandbox_uid=str(view.uid),
            kind=view.kind,
        )
        if str(view.uid) not in self._clients:
            self._clients[str(view.uid)] = self._service.runner(destination)
        return self._clients[str(view.uid)]

    async def close(self) -> None:
        await self._service.close()


# gazelle:include_dep @pypi//protobuf
