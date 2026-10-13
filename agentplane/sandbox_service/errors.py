"""Sandbox Service domain errors shared with the browser API."""


class InventoryError(Exception):
    """Base of the errors the API maps to status codes."""


class SandboxNotFoundError(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no Agentplane sandbox {name=}")
        self.name = name


class SandboxConflictError(InventoryError):
    """A name belongs to another Create."""


class SandboxRunningError(InventoryError):
    """Deletion is refused while the sandbox runs; the message is what the UI shows the operator."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name} is running; suspend it before deleting it")


class SandboxTearingDownError(InventoryError):
    """DeleteSandbox has begun: the Sandbox takes no new Sessions or commands and keeps its mode."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name} is being deleted")


class ConsoleUnavailableError(InventoryError):
    """The Sandbox has no VM whose serial console can be attached: a Pod Sandbox, a stopped VM, or a
    console another client already holds."""

    def __init__(self, name: str, *, status: int) -> None:
        super().__init__(f"sandbox {name} has no attachable VM console: Kubernetes answered {status}")
