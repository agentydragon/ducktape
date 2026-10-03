"""Delegated cgroup-v2 limits for guest runner work."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_CONTROLLERS = ("cpu", "io", "memory", "pids")
_IO_LIMIT = "rbps=52428800 wbps=20971520 riops=1000 wiops=500"
_AGGREGATE_LIMITS = {
    "cpu.max": "400000 100000",
    "memory.max": str(4 * 1024**3),
    "memory.oom.group": "1",
    "memory.swap.max": "0",
    "pids.max": "768",
}
_PROCESS_LIMITS = {
    "cpu.max": "300000 100000",
    "memory.max": str(3 * 1024**3),
    "memory.oom.group": "1",
    "memory.swap.max": "0",
    "pids.max": "512",
}


@dataclass(frozen=True, slots=True)
class AgentCgroups:
    """A delegated aggregate cgroup and the identity used for agent subprocesses."""

    root: Path
    agent_uid: int
    agent_gid: int
    block_devices: tuple[Path, ...]

    def prepare(self) -> None:
        """Create the aggregate group and fail startup unless every limit is enforceable."""
        parent = self.root.parent
        self._enable_controllers(parent)
        self.root.mkdir(mode=0o750, exist_ok=True)
        self._enable_controllers(self.root)
        devices = self._block_device_ids()
        if not devices:
            raise RuntimeError("agent cgroup requires the state and workspace block devices")
        self._write_limits(self.root, _AGGREGATE_LIMITS, devices)
        # The state-owner lock is acquired before prepare(), so any leftover process groups are
        # from a supervisor that already exited. Fence them before allowing new native work.
        for child in self.root.glob("process-*"):
            if not child.is_dir():
                raise RuntimeError(f"unexpected entry in agent cgroup root: {child}")
            self.destroy_process_group(child)

    def create_process_group(self) -> Path:
        path = self.root / f"process-{uuid4().hex}"
        path.mkdir(mode=0o750)
        try:
            self._write_limits(path, _PROCESS_LIMITS, self._block_device_ids())
        except BaseException:
            path.rmdir()
            raise
        return path

    @staticmethod
    def destroy_process_group(path: Path) -> None:
        """Kill any remaining descendants and remove the group after they leave it."""
        if not path.exists():
            return
        kill = path / "cgroup.kill"
        if kill.exists():
            kill.write_text("1\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = (path / "cgroup.events").read_text()
            if "populated 0" in events:
                path.rmdir()
                return
            time.sleep(0.05)
        raise RuntimeError(f"agent cgroup still has processes after cgroup.kill: {path}")

    def _block_device_ids(self) -> tuple[str, ...]:
        identities = []
        for path in self.block_devices:
            info = path.stat()
            if not stat.S_ISBLK(info.st_mode):
                raise RuntimeError(f"agent I/O limit target is not a block device: {path}")
            identities.append(f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}")
        return tuple(identities)

    @staticmethod
    def _enable_controllers(group: Path) -> None:
        available = set((group / "cgroup.controllers").read_text().split())
        missing = set(_CONTROLLERS) - available
        if missing:
            raise RuntimeError(f"agent cgroup controllers unavailable at {group}: {sorted(missing)}")
        enabled = set((group / "cgroup.subtree_control").read_text().split())
        requested = [name for name in _CONTROLLERS if name not in enabled]
        if requested:
            (group / "cgroup.subtree_control").write_text(" ".join(f"+{name}" for name in requested) + "\n")

    @staticmethod
    def _write_limits(group: Path, limits: dict[str, str], devices: tuple[str, ...]) -> None:
        for name, value in limits.items():
            target = group / name
            if not target.exists():
                raise RuntimeError(f"required agent cgroup control is absent: {target}")
            target.write_text(value + "\n")
        target = group / "io.max"
        if not target.exists():
            raise RuntimeError(f"required agent cgroup control is absent: {target}")
        with target.open("wb", buffering=0) as control:
            for device in devices:
                control.write(f"{device} {_IO_LIMIT}\n".encode())
