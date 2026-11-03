import subprocess
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.firecracker import storage
from agentplane.firecracker.config import Config, WorkspaceKind


def make_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pod_uid: str = "pod-1") -> storage.Storage:
    monkeypatch.setattr(storage, "STATE", tmp_path / "state")
    monkeypatch.setattr(storage, "WORK", tmp_path / "state/firecracker")
    monkeypatch.setattr(storage, "WORKSPACE", tmp_path / "workspace/disk.img")
    storage.WORK.mkdir(parents=True)
    storage.WORKSPACE.parent.mkdir()
    binary = tmp_path / "firecracker"
    binary.write_bytes(b"pinned VMM")
    config = Config(
        pod_uid=pod_uid,
        sandbox_uid="sandbox-1",
        vcpu_count=1,
        memory_mib=256,
        boot_image="ghcr.io/boot@sha256:abc",
        runner_image="ghcr.io/runner@sha256:def",
        workspace_kind=WorkspaceKind.RAW_DISK_IMAGE,
    )
    instance = storage.Storage(config, binary)
    instance.live.mkdir()
    for name, path in instance.paths().items():
        path.write_bytes(name.encode())
    return instance


def test_checkpoint_restores_matching_memory_and_disks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_storage(tmp_path, monkeypatch)
    memory = tmp_path / "memory"
    vmstate = tmp_path / "vmstate"
    memory.write_bytes(b"RAM at suspended boundary")
    vmstate.write_bytes(b"VMM at suspended boundary")
    instance.mark_active()
    manifest = instance.checkpoint(memory, vmstate, "boot-1")
    instance.clear_active()

    instance.paths()["state"].write_bytes(b"later state")
    instance.paths()["workspace"].write_bytes(b"later workspace")
    instance.mark_active()
    loaded = instance.read_manifest()
    assert loaded is not None
    instance.restore(loaded)
    assert instance.paths()["state"].read_bytes() == b"state"
    assert instance.paths()["workspace"].read_bytes() == b"workspace"
    assert (storage.WORK / manifest.generation / "memory").read_bytes() == b"RAM at suspended boundary"
    with pytest.raises(RuntimeError, match="uncertain"):
        instance.mark_active()

    instance.clear_active()
    second = instance.checkpoint(memory, vmstate, "boot-1")
    capture = storage.WORK / ".capture"
    capture.mkdir()
    instance.finish_checkpoint(second, capture)
    assert not (storage.WORK / manifest.generation).exists()
    assert not capture.exists()
    assert instance.read_manifest() == second


def test_partial_checkpoint_cannot_replace_valid_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_storage(tmp_path, monkeypatch)
    memory = tmp_path / "memory"
    vmstate = tmp_path / "vmstate"
    memory.write_bytes(b"ram")
    vmstate.write_bytes(b"state")
    first = instance.checkpoint(memory, vmstate, "boot-1")
    original_copy = storage.copy_durable

    def fail_vmstate(source: Path, destination: Path) -> storage.FileRecord:
        if source == vmstate:
            raise OSError("out of space")
        return original_copy(source, destination)

    monkeypatch.setattr(storage, "copy_durable", fail_vmstate)
    with pytest.raises(OSError, match="out of space"):
        instance.checkpoint(memory, vmstate, "boot-1")
    assert instance.read_manifest() == first


def test_checkpoint_is_bound_to_pod_and_vmm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_storage(tmp_path, monkeypatch)
    memory = tmp_path / "memory"
    vmstate = tmp_path / "vmstate"
    memory.write_bytes(b"ram")
    vmstate.write_bytes(b"state")
    instance.checkpoint(memory, vmstate, "boot-1")
    instance.config = instance.config.model_copy(update={"pod_uid": "pod-2"})
    with pytest.raises(ValueError, match="different Pod"):
        instance.read_manifest()


def test_existing_ext4_workspace_block_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device = tmp_path / "workspace-block"
    device.write_bytes(b"existing ext4 data")
    monkeypatch.setattr(Path, "is_block_device", lambda path: path == device)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "ext4\n", "")

    monkeypatch.setattr(storage.subprocess, "run", run)
    storage.prepare_workspace_block(device)
    assert device.read_bytes() == b"existing ext4 data"
    assert [command[0] for command in commands] == ["blkid"]


@pytest.mark.parametrize(("data", "detected_type"), [(b"\0" * 4096 + b"owner data", None), (b"xfs data", "xfs")])
def test_workspace_block_refuses_other_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes, detected_type: str | None
) -> None:
    device = tmp_path / "workspace-block"
    device.write_bytes(data)
    monkeypatch.setattr(Path, "is_block_device", lambda path: path == device)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(
            args, 0 if detected_type else 2, f"{detected_type}\n" if detected_type else "", ""
        )

    monkeypatch.setattr(storage.subprocess, "run", run)
    with pytest.raises(ValueError, match=r"non-ext4|unsigned nonzero"):
        storage.prepare_workspace_block(device)
    assert device.read_bytes() == data
    assert [command[0] for command in commands] == ["blkid"]


def test_zero_workspace_block_is_formatted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device = tmp_path / "workspace-block"
    device.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(Path, "is_block_device", lambda path: path == device)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[0] == "mkfs.ext4":
            device.write_bytes(b"formatted ext4")
            return subprocess.CompletedProcess(args, 0, "", "")
        if device.read_bytes() == b"formatted ext4":
            return subprocess.CompletedProcess(args, 0, "ext4\n", "")
        return subprocess.CompletedProcess(args, 2, "", "")

    monkeypatch.setattr(storage.subprocess, "run", run)
    storage.prepare_workspace_block(device)
    assert device.read_bytes() == b"formatted ext4"
    assert [command[0] for command in commands] == ["blkid", "mkfs.ext4", "blkid"]


if __name__ == "__main__":
    pytest_bazel.main()
