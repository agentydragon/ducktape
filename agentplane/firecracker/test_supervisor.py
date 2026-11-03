from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel

from agentplane.firecracker import supervisor
from agentplane.firecracker.config import Config, WorkspaceKind
from agentplane.firecracker.network import Network
from agentplane.firecracker.storage import Manifest, Storage


def test_thaw_precedes_network_and_checkpoint_precedes_vmm_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    config = Config(
        pod_uid="pod-1",
        sandbox_uid="sandbox-1",
        vcpu_count=1,
        memory_mib=256,
        boot_image="boot-ref",
        runner_image="runner-ref",
        workspace_kind=WorkspaceKind.RAW_DISK_IMAGE,
    )

    class FakeStorage:
        active_path = tmp_path / "active"

        def mark_active(self) -> None:
            events.append("active")

        def clear_active(self) -> None:
            events.append("clear-active")

        def checkpoint(self, memory: Path, vmstate: Path, boot_id: str) -> None:
            events.append("checkpoint")

        def finish_checkpoint(self, manifest: object, capture: Path) -> None:
            events.append("finish-checkpoint")

        def restore(self, manifest: Manifest) -> None:
            events.append("restore")

    class FakeNetwork:
        def enable(self) -> None:
            events.append("enable")

        def disable(self) -> None:
            events.append("disable")

    class FakeChild:
        def poll(self) -> None:
            return None

    @contextmanager
    def fake_process(binary: Path, api_path: Path, log_path: Path) -> Iterator[FakeChild]:
        api_path.touch()
        events.append("spawn")
        try:
            yield FakeChild()
        finally:
            events.append("vmm-exit")

    class FakeApi:
        def __init__(self, socket_path: Path) -> None:
            pass

        def request(self, method: str, route: str, payload: dict[str, Any]) -> dict[str, Any]:
            events.append(route)
            return {}

    def fake_sync_guest(path: Path, stopped: object, expected_boot_id: str) -> str:
        events.append("thaw")
        return "boot-1"

    intents = iter([True, False])
    monkeypatch.setattr(supervisor, "API_SOCKET", tmp_path / "api.sock")
    monkeypatch.setattr(supervisor, "VSOCK_SOCKET", tmp_path / "vsock")
    monkeypatch.setattr(supervisor, "WORK", tmp_path)
    monkeypatch.setattr(supervisor, "process", fake_process)
    monkeypatch.setattr(supervisor, "FirecrackerApi", FakeApi)
    monkeypatch.setattr(supervisor, "read_intent", lambda path: next(intents))
    monkeypatch.setattr(supervisor, "sync_guest", fake_sync_guest)
    monkeypatch.setattr(supervisor, "guest_reachable", lambda: True)
    instance = supervisor.Supervisor(config, cast(Storage, FakeStorage()), cast(Network, FakeNetwork()))
    monkeypatch.setattr(instance, "configure", lambda api: events.append("configure"))
    instance.run_continuation(None)
    assert events == [
        "active",
        "spawn",
        "configure",
        "thaw",
        "enable",
        "disable",
        "/vm",
        "/snapshot/create",
        "checkpoint",
        "vmm-exit",
        "finish-checkpoint",
        "clear-active",
    ]
    assert instance.phase == supervisor.Phase.SUSPENDED

    events.clear()
    manifest = Manifest(
        pod_uid="pod-1",
        sandbox_uid="sandbox-1",
        boot_id="different-boot",
        config_sha256="a",
        firecracker_sha256="b",
        generation="checkpoint-test",
        files={},
    )
    with pytest.raises(RuntimeError, match="boot ID"):
        instance.run_continuation(manifest)
    assert events == ["active", "spawn", "restore", "/snapshot/load", "thaw", "vmm-exit"]


if __name__ == "__main__":
    pytest_bazel.main()
