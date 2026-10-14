"""Sandbox domain errors shared by every requester and reader of Sandboxes."""


class InventoryError(Exception):
    """Base of the errors the API maps to status codes."""


class SandboxNotFoundError(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no Agentplane sandbox {name=}")
        self.name = name


class SandboxConflictError(InventoryError):
    """A name belongs to another Create."""


class InvalidSandboxError(InventoryError, ValueError):
    """A Create request that names no valid Sandbox."""


class SandboxRunningError(InventoryError):
    """Deletion is refused while the sandbox runs; the message is what the UI shows the operator."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name} is running; suspend it before deleting it")


class SandboxTearingDownError(InventoryError):
    """DeleteSandbox has begun: the Sandbox takes no new Sessions or commands and keeps its mode."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name} is being deleted")


class SandboxQuiescingError(InventoryError):
    """A suspend is sealing this incarnation; retry resume once the Sandbox is suspended."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name} is quiescing; retry resume after it is suspended")


class ConsoleUnavailableError(InventoryError):
    """The Sandbox has no VM whose serial console can be attached: a Pod Sandbox, a stopped VM, or a
    console another client already holds."""

    def __init__(self, name: str, *, status: int) -> None:
        super().__init__(f"sandbox {name} has no attachable VM console: Kubernetes answered {status}")


class UnknownPolicySetError(InventoryError):
    """A binding naming a set the namespace does not hold, which would grant nothing.

    The CRD admits any string in `spec.policySets` and the Action Service treats a name that
    resolves to nothing as contributing nothing, so a dangling name is a state the system already
    handles. This refuses one at the moment it would be written; one the operator deletes
    afterwards still lands there without granting anything from the missing set.
    """

    def __init__(self, names: list[str], *, kind: str) -> None:
        super().__init__(f"no {kind} in the namespace is named {', '.join(names)}")
        self.names = names
