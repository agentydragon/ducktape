"""Container executor protocol — abstraction over container runtimes.

Defines the ContainerExecutor protocol that AgentRegistry uses to create
and manage agent containers. Implementations exist for Docker (DockerExecutor)
and Kubernetes (K8sExecutor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


# TODO: "Pod" is k8s vocabulary — a runtime-agnostic phase deserves a runtime-agnostic
# name (e.g. ExecutionPhase); PodInfo likewise.
class PodPhase(StrEnum):
    """Runtime-agnostic pod lifecycle phase. Maps k8s pod phases directly; the Docker
    executor maps container states onto the same set."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PodInfo:
    """Snapshot of a running/terminal agent container, as observed from the runtime.

    The GraderSupervisor reconciles against this (listed by label) rather than
    in-memory handles, so it survives backend restarts and can adopt/reap
    containers a previous instance started.
    """

    name: str
    image: str
    phase: PodPhase
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


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
        self,
        *,
        name: str,
        image_id: str,
        env: dict[str, str],
        labels: dict[str, str],
        annotations: dict[str, str] | None = None,
    ) -> ContainerHandle:
        """Create and start a container/pod. Returns a handle for lifecycle management.

        `annotations` carry metadata that may not be a valid label value (e.g. a
        snapshot slug containing '/'). Kubernetes stores them as pod annotations;
        Docker (which has no annotations) folds them into labels.
        """
        ...

    async def list_pods(self, label_selector: dict[str, str]) -> list[PodInfo]:
        """List containers/pods matching all of the given labels."""
        ...

    def handle_for(self, name: str) -> ContainerHandle:
        """Build a handle for an already-running container/pod by name.

        Used to adopt or delete a container that this process did not start
        (e.g. a grader left running by a previous backend instance).
        """
        ...

    async def close(self) -> None:
        """Release runtime resources (API clients, connections)."""
        ...
