"""Prepare and verify the VM workspace disk without risking unrecognized contents.

NixOS's ``fileSystems.<name>.autoFormat`` only asks blkid whether a filesystem is present. The
workspace contract is stronger: first use may format a disk only if every byte is zero, and a later
boot may mount only the expected ext4 filesystem and label. Keep that policy here and the mount in
the NixOS ``fileSystems`` module.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

CHUNK_BYTES = 16 << 20
DEVICE_WAIT_SECONDS = 60
FILESYSTEM = "ext4"
LABEL = "APWORKSPACE"

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class WorkspaceDiskError(RuntimeError):
    """The workspace disk is unreadable or does not match its declared layout."""


def first_nonzero_offset(device: Path, *, chunk_bytes: int = CHUNK_BYTES) -> int | None:
    """Return the first non-zero byte offset, or None if the device reads as zero end to end."""
    zeros = bytes(chunk_bytes)
    offset = 0
    with device.open("rb", buffering=0) as stream:
        while chunk := stream.read(chunk_bytes):
            if chunk != zeros[: len(chunk)]:
                return offset + len(chunk) - len(chunk.lstrip(b"\0"))
            offset += len(chunk)
    return None


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, check=False, text=True)


def _checked_output(command: Sequence[str], *, run_command: CommandRunner) -> str:
    result = run_command(command)
    if result.returncode != 0:
        raise WorkspaceDiskError(
            f"Could not inspect the workspace disk with {' '.join(command)} (status {result.returncode})"
        )
    return result.stdout.strip()


def prepare_workspace_disk(
    device: Path,
    *,
    run_command: CommandRunner = _run,
    find_nonzero: Callable[[Path], int | None] = first_nonzero_offset,
) -> bool:
    """Validate an existing workspace filesystem or format an entirely zero-filled disk.

    Return True only when a new filesystem was created. Dependencies are injectable so tests can
    exercise the first-use and remount contract without a privileged block device.
    """
    device_name = str(device)
    probe = run_command(["blkid", "-p", "-s", "TYPE", "-o", "value", device_name])
    if probe.returncode == 0:
        filesystem = probe.stdout.strip()
        label = _checked_output(["blkid", "-p", "-s", "LABEL", "-o", "value", device_name], run_command=run_command)
        signatures = _checked_output(
            ["wipefs", "--no-act", "--noheadings", "--output", "TYPE", device_name], run_command=run_command
        ).splitlines()
        if filesystem == FILESYSTEM and label == LABEL and signatures == [FILESYSTEM]:
            return False
        raise WorkspaceDiskError(
            "Refusing unexpected filesystem on the workspace disk "
            f"({filesystem}, label {label}, signatures {signatures})"
        )
    if probe.returncode != 2:
        raise WorkspaceDiskError(f"Could not inspect the workspace disk (blkid status {probe.returncode})")

    signature_output = _checked_output(["wipefs", "--no-act", "--noheadings", device_name], run_command=run_command)
    if signature_output:
        raise WorkspaceDiskError("Refusing unrecognized disk signatures on the workspace disk")

    offset = find_nonzero(device)
    if offset is not None:
        raise WorkspaceDiskError(
            "Refusing to format a non-zero workspace disk without a recognized filesystem "
            f"(first non-zero byte at offset {offset})"
        )

    result = run_command(["mkfs.ext4", "-F", "-L", LABEL, device_name])
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise WorkspaceDiskError(
            f"Could not format the blank workspace disk (status {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: agentplane-prepare-workspace DEVICE", file=sys.stderr)
        return 2

    device = Path(args[0])
    for _ in range(DEVICE_WAIT_SECONDS):
        if device.is_block_device():
            break
        time.sleep(1)
    else:
        print(
            f"KubeVirt workspace disk did not appear at {device} within {DEVICE_WAIT_SECONDS} seconds", file=sys.stderr
        )
        return 1

    try:
        prepare_workspace_disk(device)
    except (OSError, WorkspaceDiskError) as error:
        print(f"Workspace disk initialization failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
