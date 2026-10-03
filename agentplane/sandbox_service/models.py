"""Sandbox state vocabulary and domain errors shared with the browser API."""

from enum import StrEnum


class OperatingMode(StrEnum):
    RUNNING = "Running"
    SUSPENDED = "Suspended"


class ProvisioningState(StrEnum):
    WAITING_FOR_GRANTS = "waiting_for_grants"
    WAITING_FOR_POD = "waiting_for_pod"
    WAITING_FOR_POD_READY = "waiting_for_pod_ready"
    RUNNING = "running"
    SUSPENDED = "suspended"
    WAITING_FOR_VM = "waiting_for_vm"
    WAITING_FOR_GUEST = "waiting_for_guest"
    STOPPING = "stopping"


class EnvironmentKind(StrEnum):
    AGENT_SANDBOX = "agent_sandbox"
    KUBEVIRT = "kubevirt"


def environment_kind(value: str) -> EnvironmentKind:
    return EnvironmentKind(value)


class InventoryError(Exception):
    """Base of the errors the API maps to status codes."""


class SandboxNotFoundError(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no Agentplane sandbox {name=}")
        self.name = name


class SandboxRunningError(InventoryError):
    """Deletion is refused while the sandbox runs; the message is what the UI shows the operator."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name} is running; suspend it before deleting it")
