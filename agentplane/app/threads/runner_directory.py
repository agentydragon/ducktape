"""Resolve a live Sandbox's current runner destination for its control Service."""

# gazelle:include_dep @pypi//protobuf

from agentplane.app.live import LiveIndex
from agentplane.app.sandbox_models import sandbox_is_ready
from agentplane.proto.sandboxes_pb2 import SandboxDestination, ServiceAccount
from agentplane.runner_gateway.client import SandboxControlClient, SandboxRunner
from agentplane.sandboxes.errors import SandboxNotFoundError


class SandboxNotReachableError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name=} has no ready, authorized Pod")
        self.name = name


class RunnerDirectory:
    def __init__(self, index: LiveIndex, control: SandboxControlClient) -> None:
        self._index = index
        self.control = control
        self._clients: dict[str, SandboxRunner] = {}

    def client(self, sandbox: str) -> SandboxRunner:
        view = self._index.sandbox_view(sandbox)
        if view is None:
            raise SandboxNotFoundError(sandbox)
        if not sandbox_is_ready(view):
            raise SandboxNotReachableError(sandbox)
        destination = SandboxDestination(
            owner=ServiceAccount(namespace=view.service_account.namespace, name=view.service_account.name),
            sandbox=view.name,
            sandbox_uid=str(view.uid),
        )
        if str(view.uid) not in self._clients:
            self._clients[str(view.uid)] = self.control.runner(destination)
        return self._clients[str(view.uid)]
