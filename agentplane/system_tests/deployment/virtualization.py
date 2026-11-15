"""Runtime capabilities selected by a system-test fixture."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.k3s.baseline import K3s


class RetainedInfrastructure(Protocol):
    resources: list[dict[str, Any]]

    def baseline_objects(self) -> list[dict[str, Any]]: ...

    def baseline_helpers(self) -> dict[str, Path]: ...

    def identity(self, k3s: K3s) -> dict[str, str]: ...

    def install(self, k3s: K3s) -> None: ...

    def wait_for_apis(self, k3s: K3s) -> None: ...

    def wait_until_ready(self, k3s: K3s) -> None: ...


class WorkspaceStorage(Protocol):
    @property
    def closed(self) -> bool: ...

    @property
    def released_devices(self) -> int: ...

    def __enter__(self) -> WorkspaceStorage: ...

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None: ...

    def storage_class_uid(self, k3s: K3s) -> str: ...

    def provisioning(self, cluster: Cluster) -> AbstractContextManager[None]: ...

    def clean(self, k3s: K3s) -> None: ...


class GuestLifecycle(Protocol):
    setup_objects: list[dict[str, Any]]

    def configure(self, objects: list[dict[str, Any]]) -> None: ...

    def install(self, k3s: K3s) -> None: ...

    def wait_for_apis(self, k3s: K3s) -> None: ...

    def wait_until_ready(self, k3s: K3s) -> None: ...

    def capture(
        self, k3s: K3s, namespace: str, validate_shutdown: Callable[[], bool]
    ) -> AbstractContextManager[None]: ...

    def report(self, k3s: K3s, namespace: str) -> str: ...

    def clean(self, k3s: K3s) -> None: ...


@dataclass(frozen=True)
class RuntimeCapabilities:
    guest: GuestLifecycle
    retained_infrastructure: RetainedInfrastructure | None = None
    workspace_storage: WorkspaceStorage | None = None


RuntimeFactory = Callable[[Path], RuntimeCapabilities]
