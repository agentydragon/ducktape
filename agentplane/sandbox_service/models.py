"""Sandbox Service domain errors shared with the browser API."""


class InventoryError(Exception):
    """Base of the errors the API maps to status codes."""


class SandboxNotFoundError(InventoryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no Agentplane sandbox {name=}")
        self.name = name


class SandboxConflictError(InventoryError):
    """A name belongs to another Create or initialization is still in progress."""


class SandboxRunningError(InventoryError):
    """Deletion is refused while the sandbox runs; the message is what the UI shows the operator."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name} is running; suspend it before deleting it")


class RetentionHeldError(InventoryError):
    """Deletion is refused while a retention hold has not confirmed its Session through the seal."""

    def __init__(self, name: str) -> None:
        super().__init__(f"sandbox {name} has unsatisfied retention holds; the sandbox lists them")
        self.name = name


class HoldNotFoundError(InventoryError):
    def __init__(self, session_id: str, holder: str) -> None:
        super().__init__(f"no retention hold {session_id=} {holder=}")
