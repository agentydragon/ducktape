"""A SandboxTemplate and working directory selected from a running deployment."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Executor:
    template: str
    cwd: str
