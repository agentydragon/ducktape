"""A Sandbox backend's optional lifecycle extension to workload orchestration."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.k3s.baseline import K3s


class Virtualization(Protocol):
    def __enter__(self) -> Virtualization: ...

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None: ...

    releases: list[dict[str, Any]]

    @property
    def closed(self) -> bool: ...

    @property
    def released_devices(self) -> int: ...

    def baseline_objects(self) -> list[dict[str, Any]]: ...

    def baseline_helpers(self) -> dict[str, Path]: ...

    def baseline_identity(self, k3s: K3s) -> dict[str, str]: ...

    def storage_class_uid(self, k3s: K3s) -> str: ...

    def configure(self, objects: list[dict[str, Any]]) -> None: ...

    def install(self, k3s: K3s) -> None: ...

    def wait_for_apis(self, k3s: K3s) -> None: ...

    def wait_until_ready(self, k3s: K3s) -> None: ...

    def provisioning(self, cluster: Cluster) -> AbstractContextManager[None]: ...

    def capture(
        self, k3s: K3s, namespace: str, validate_shutdown: Callable[[], bool]
    ) -> AbstractContextManager[None]: ...

    def report(self, k3s: K3s, namespace: str) -> str: ...

    def clean(self, k3s: K3s) -> None: ...


VirtualizationFactory = Callable[[Path], Virtualization]
