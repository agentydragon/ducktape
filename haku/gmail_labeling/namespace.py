"""The label-namespace policy that enforces the closure invariant."""

from dataclasses import dataclass

from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class LabelNamespace:
    """A Gmail label namespace defined by a required name prefix.

    Every label this server reads or mutates must have a display name starting
    with `prefix`. This is the closure invariant: the set of labels the server
    can touch is exactly the labels under `prefix`, so system labels (INBOX,
    TRASH, …) and the user's other labels are structurally unreachable.
    """

    prefix: str

    def __post_init__(self) -> None:
        if not self.prefix:
            raise ValueError("LabelNamespace.prefix must be non-empty")

    def allows(self, name: str) -> bool:
        return name.startswith(self.prefix)

    def require(self, name: str) -> None:
        """Raise `ToolError` if `name` is outside the namespace."""
        if not self.allows(name):
            raise ToolError(
                f"label {name!r} is outside the managed namespace {self.prefix!r}; "
                f"this server only manages labels whose name starts with {self.prefix!r}"
            )
