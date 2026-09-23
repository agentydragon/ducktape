"""The runner in each sandbox, as the cluster index sees it: which sandboxes run one, and a client to
reach each, cached per address."""

from __future__ import annotations

import asyncio

from agentplane.app.changes import Changes
from agentplane.app.inventory import ProvisioningState, SandboxNotFoundError
from agentplane.app.live import LiveIndex
from agentplane.runner.client import RunnerClient


class SandboxNotReachableError(Exception):
    def __init__(self, name: str, state: ProvisioningState) -> None:
        super().__init__(f"sandbox {name=} has no reachable runner: it is {state}")
        self.name = name


class Runners:
    def __init__(self, index: LiveIndex, port: int) -> None:
        self._index = index
        self._port = port
        self._clients: dict[str, RunnerClient] = {}

    @property
    def changes(self) -> Changes:
        """Wakes when the running set may have changed; it is the index's own signal."""
        return self._index.changes

    def running(self) -> set[str]:
        return {view.name for view in self._index.sandbox_views() if view.state is ProvisioningState.RUNNING}

    def client(self, sandbox: str) -> RunnerClient:
        view = self._index.sandbox_view(sandbox)
        if view is None:
            raise SandboxNotFoundError(sandbox)
        if view.state is not ProvisioningState.RUNNING or view.pod is None or view.pod.ip is None:
            raise SandboxNotReachableError(sandbox, view.state)
        address = f"{view.pod.ip}:{self._port}"
        if address not in self._clients:
            self._clients[address] = RunnerClient(address)
        return self._clients[address]

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self._clients.values()))
