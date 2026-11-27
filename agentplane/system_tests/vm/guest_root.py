"""The VM guest's root disk attached as a read-only KubeVirt host disk instead of an imported containerDisk.

The rendered fixture names `HOST_PATH`; the stack bind-mounts the staged EROFS image there read-only, so the
557 MB disk is neither imported into containerd nor copied (issue #20575).
"""

import contextlib
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

from python.runfiles import Runfiles

HOST_PATH = Path("/var/lib/agentplane-guest-root/root.img")
_RLOCATION_ENV = "VM_GUEST_ROOT_DISK_RLOCATION"


def staged_disk() -> Path | None:
    """The staged guest root image, when this test attaches it as a host disk."""
    rlocation = os.environ.get(_RLOCATION_ENV)
    if rlocation is None:
        return None
    path = Runfiles.CreateOrRaise().Rlocation(rlocation)
    if path is None:
        raise FileNotFoundError(f"guest root disk is missing from runfiles: {rlocation=}")
    return Path(path).resolve(strict=True)


def referenced_by(objects: list[dict]) -> bool:
    """Whether any rendered VM template attaches `HOST_PATH` as a host disk."""
    return any(
        volume.get("hostDisk", {}).get("path") == str(HOST_PATH)
        for doc in objects
        if doc["kind"] == "SandboxTemplate" and "vm" in doc["spec"]
        for volume in doc["spec"]["vm"]["template"]["spec"]["volumes"]
    )


@contextlib.contextmanager
def mounted(staged: Path) -> Iterator[None]:
    """Expose `staged` at `HOST_PATH` read-only for the test's K3s node; nothing is copied."""
    HOST_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOST_PATH.touch()
    subprocess.run(["mount", "--bind", staged, HOST_PATH], check=True)
    try:
        subprocess.run(["mount", "-o", "remount,bind,ro", HOST_PATH], check=True)
        yield
    finally:
        subprocess.run(["umount", HOST_PATH], check=True)
