"""Container executor protocol — abstraction over container runtimes.

Defines the ContainerExecutor protocol that AgentRegistry uses to create
and manage agent containers. Implementations exist for Docker (DockerExecutor)
and Kubernetes (K8sExecutor).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Exited:
    """Container exited normally (possibly with non-zero exit code)."""

    exit_code: int


@dataclass(frozen=True)
class TimedOut:
    """Container was killed after exceeding its timeout."""


ExitStatus = Exited | TimedOut


@dataclass(frozen=True)
class ContainerResult:
    """Result of running an agent container."""

    stdout: str
    stderr: str
    exit: ExitStatus


class ContainerHandle(Protocol):
    """Handle to a running container/pod. Returned by ContainerExecutor.run_container.

    Owns the lifecycle of a single container: wait for completion and kill/delete.
    """

    @property
    def name(self) -> str: ...

    async def wait(self, *, timeout_seconds: int | None) -> ContainerResult:
        """Wait for container to exit, capturing stdout/stderr.

        If timeout_seconds is not None and the container doesn't exit in time,
        kills the container and returns a ContainerResult with exit=TimedOut().
        """
        ...

    async def kill_and_delete(self) -> None:
        """Kill and remove a container/pod. Best-effort — logs warnings on failure."""
        ...


class ContainerExecutor(Protocol):
    """Abstraction over container runtimes (Docker, Kubernetes).

    AgentRegistry delegates container creation to this protocol.
    Lifecycle operations (wait, kill) live on the returned ContainerHandle.
    """

    async def ensure_image(self, image_ref: str) -> str:
        """Ensure image is available to the runtime. Returns runtime-specific image ID.

        For Docker: inspects local cache, pulls if missing.
        For Kubernetes: no-op (kubelet pulls on pod creation), returns image_ref as-is.
        """
        ...

    async def run_container(
        self, *, name: str, image_id: str, env: dict[str, str], labels: dict[str, str]
    ) -> ContainerHandle:
        """Create and start a container/pod. Returns a handle for lifecycle management."""
        ...

    async def close(self) -> None:
        """Release runtime resources (API clients, connections)."""
        ...
