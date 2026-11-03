"""Disk preparation and durable, same-Pod RAM checkpoint ownership."""

import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentplane.firecracker.config import Config, WorkspaceKind
from agentplane.firecracker.credentials import read_credentials

STATE = Path("/var/lib/agentplane/state")
WORKSPACE = Path("/var/lib/agentplane/workspace/disk.img")
WORKSPACE_BLOCK = Path("/dev/agentplane-workspace")
WORK = STATE / "firecracker"


class FileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: int
    sha256: str


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pod_uid: str
    sandbox_uid: str
    boot_id: str
    config_sha256: str
    firecracker_sha256: str
    generation: str
    files: dict[str, FileRecord]


def digest(path: Path) -> FileRecord:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            hasher.update(data)
            size += len(data)
    return FileRecord(size=size, sha256=hasher.hexdigest())


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_durable(source: Path, destination: Path) -> FileRecord:
    if source.is_block_device():
        with source.open("rb") as reader, destination.open("xb") as writer:
            while data := reader.read(1024 * 1024):
                if any(data):
                    writer.write(data)
                else:
                    writer.seek(len(data), os.SEEK_CUR)
            writer.truncate(writer.tell())
            writer.flush()
            os.fsync(writer.fileno())
        destination.chmod(0o600)
    else:
        subprocess.run(["cp", "--reflink=auto", "--sparse=always", "--", str(source), str(destination)], check=True)
        destination.chmod(0o600)
        with destination.open("rb") as copy:
            os.fsync(copy.fileno())
    return digest(destination)


def create_ext4(destination: Path, size: str, label: str, source: Path | None = None) -> None:
    if destination.exists():
        return
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}")
    subprocess.run(["truncate", "-s", size, str(staging)], check=True)
    command = ["mkfs.ext4", "-F", "-q", "-m", "0", "-L", label]
    if source is not None:
        command += ["-d", str(source)]
    subprocess.run([*command, str(staging)], check=True)
    with staging.open("rb") as image:
        os.fsync(image.fileno())
    staging.replace(destination)
    sync_dir(destination.parent)


def create_iso(destination: Path, label: str, source: Path) -> None:
    if destination.exists():
        return
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}")
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-quiet", "-R", "-J", "-V", label, "-o", str(staging), str(source)], check=True
    )
    with staging.open("rb") as image:
        os.fsync(image.fileno())
    staging.replace(destination)
    sync_dir(destination.parent)


def prepare_workspace_block(device: Path) -> None:
    if not device.is_block_device():
        raise ValueError(f"workspace block device is missing: {device}")
    probe = subprocess.run(
        ["blkid", "-p", "-s", "TYPE", "-o", "value", str(device)], check=False, capture_output=True, text=True
    )
    if probe.returncode == 0:
        if probe.stdout.strip() != "ext4":
            raise ValueError(f"workspace block device has a non-ext4 signature: {device}")
        return
    if probe.returncode != 2:
        raise RuntimeError(f"cannot inspect workspace block device: {device}: {probe.stderr.strip()}")
    with device.open("rb") as image:
        while block := image.read(4 * 1024 * 1024):
            if any(block):
                raise ValueError(f"workspace block device has unsigned nonzero data: {device}")
    subprocess.run(["mkfs.ext4", "-F", "-q", "-m", "0", "-L", "APWORKSPACE", str(device)], check=True)
    with device.open("r+b") as image:
        os.fsync(image.fileno())
    formatted = subprocess.run(
        ["blkid", "-p", "-s", "TYPE", "-o", "value", str(device)], check=False, capture_output=True, text=True
    )
    if formatted.returncode != 0 or formatted.stdout.strip() != "ext4":
        raise RuntimeError(f"workspace block format did not produce ext4: {device}")


@contextmanager
def owner_lock() -> Iterator[None]:
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "owner.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Storage:
    def __init__(self, config: Config, firecracker: Path) -> None:
        self.config = config
        self.firecracker = firecracker
        self.live = WORK / "live"
        self.manifest_path = WORK / "manifest.json"
        self.active_path = WORK / "active"

    def paths(self) -> dict[str, Path]:
        return {
            "kernel": self.live / "vmlinux",
            "rootfs": self.live / "rootfs.ext4",
            "runner": self.live / "runner.img",
            "state": STATE / "disk.img",
            "workspace": WORKSPACE if self.config.workspace_kind == WorkspaceKind.RAW_DISK_IMAGE else WORKSPACE_BLOCK,
            "config": self.live / "config.iso",
            "launch": self.live / "launch.iso",
            "trust": self.live / "trust.iso",
            "credentials": self.live / "credentials.ext4",
        }

    def prepare(self) -> None:
        if self.manifest_path.exists() or self.active_path.exists():
            return
        self.live.mkdir(parents=True, exist_ok=True)
        paths = self.paths()
        for name, source in {
            "kernel": self.config.boot_directory / "vmlinux",
            "rootfs": self.config.boot_directory / "rootfs.ext4",
            "runner": self.config.runner_directory / "disk/runner.img",
        }.items():
            if not paths[name].exists():
                staging = paths[name].with_name(f".{paths[name].name}.{uuid.uuid4().hex}")
                copy_durable(source, staging)
                staging.replace(paths[name])
                sync_dir(self.live)
        create_ext4(paths["state"], f"{self.config.state_size_mib}M", "APSTATE")
        if self.config.workspace_kind == WorkspaceKind.RAW_DISK_IMAGE:
            WORKSPACE.parent.mkdir(parents=True, exist_ok=True)
            create_ext4(WORKSPACE, f"{self.config.workspace_size_mib}M", "APWORKSPACE")
        else:
            prepare_workspace_block(WORKSPACE_BLOCK)
        create_iso(paths["config"], "APCONFIG", Path("/run/agentplane/config"))
        create_iso(paths["launch"], "APLAUNCH", Path("/run/agentplane/sandbox"))
        create_iso(paths["trust"], "APTRUST", Path("/run/agentplane/trust"))
        with tempfile.TemporaryDirectory(prefix="credential-seed-", dir="/work") as directory:
            seed = Path(directory)
            credentials = read_credentials(Path("/run/agentplane/environment-runtime-tls"))
            for value, target in (
                (credentials.tls_cert, "tls.crt"),
                (credentials.tls_key, "tls.key"),
                (credentials.ca_bundle, "ca-certificates.crt"),
            ):
                destination = seed / target
                destination.write_text(value)
                destination.chmod(0o600)
            create_ext4(paths["credentials"], "16M", "APCREDENTIAL", seed)
        paths["credentials"].chmod(0o600)

    def config_digest(self) -> str:
        return hashlib.sha256(self.config.model_dump_json().encode()).hexdigest()

    def read_manifest(self) -> Manifest | None:
        if not self.manifest_path.exists():
            return None
        manifest = Manifest.model_validate_json(self.manifest_path.read_text())
        if manifest.pod_uid != self.config.pod_uid or manifest.sandbox_uid != self.config.sandbox_uid:
            raise ValueError("checkpoint belongs to a different Pod or Sandbox")
        if (
            manifest.config_sha256 != self.config_digest()
            or manifest.firecracker_sha256 != digest(self.firecracker).sha256
        ):
            raise ValueError("checkpoint configuration or Firecracker binary changed")
        if manifest.generation != Path(manifest.generation).name:
            raise ValueError("invalid checkpoint generation")
        return manifest

    def mark_active(self) -> None:
        if self.active_path.exists():
            raise RuntimeError("an active VM continuation has an uncertain outcome")
        with self.active_path.open("x") as marker:
            marker.write(self.config.pod_uid)
            marker.flush()
            os.fsync(marker.fileno())
        sync_dir(WORK)

    def clear_active(self) -> None:
        self.active_path.unlink()
        sync_dir(WORK)

    def restore(self, manifest: Manifest) -> None:
        generation = WORK / manifest.generation
        if set(manifest.files) != {*self.paths(), "memory", "vmstate"}:
            raise ValueError("checkpoint disk set is incomplete")
        for name, record in manifest.files.items():
            if digest(generation / name) != record:
                raise ValueError(f"checkpoint file failed verification: {name}")
        for name, target in self.paths().items():
            source = generation / name
            if target.is_block_device():
                with source.open("rb") as reader, target.open("wb") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
            else:
                staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}")
                copy_durable(source, staging)
                staging.replace(target)
                sync_dir(target.parent)

    def checkpoint(self, memory: Path, vmstate: Path, boot_id: str) -> Manifest:
        generation = f"checkpoint-{uuid.uuid4().hex}"
        directory = WORK / generation
        directory.mkdir()
        files = {name: copy_durable(path, directory / name) for name, path in self.paths().items()}
        files["memory"] = copy_durable(memory, directory / "memory")
        files["vmstate"] = copy_durable(vmstate, directory / "vmstate")
        sync_dir(directory)
        manifest = Manifest(
            pod_uid=self.config.pod_uid,
            sandbox_uid=self.config.sandbox_uid,
            boot_id=boot_id,
            config_sha256=self.config_digest(),
            firecracker_sha256=digest(self.firecracker).sha256,
            generation=generation,
            files=files,
        )
        staging = WORK / f".manifest-{uuid.uuid4().hex}"
        with staging.open("x") as output:
            output.write(manifest.model_dump_json())
            output.flush()
            os.fsync(output.fileno())
        staging.replace(self.manifest_path)
        sync_dir(WORK)
        return manifest

    def finish_checkpoint(self, manifest: Manifest, capture: Path) -> None:
        # Only the committed generation is a restore source after the VMM exits.
        shutil.rmtree(capture)
        for directory in WORK.glob("checkpoint-*"):
            if directory.name != manifest.generation:
                shutil.rmtree(directory)
        sync_dir(WORK)
