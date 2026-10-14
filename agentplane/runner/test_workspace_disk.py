import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.runner.workspace_disk import WorkspaceDiskError, first_nonzero_offset, prepare_workspace_disk

# Small enough that the cases cross chunk boundaries and end in a short final chunk.
CHUNK_BYTES = 4096
SIZE = 3 * CHUNK_BYTES + 17


class FakeDisk:
    def __init__(self, filesystem: str | None = None, label: str | None = None) -> None:
        self.filesystem = filesystem
        self.label = label
        self.signatures = [filesystem] if filesystem is not None else []
        self.commands: list[list[str]] = []

    def run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(command)
        self.commands.append(args)
        if args[0] == "blkid":
            value = self.filesystem if args[3] == "TYPE" else self.label
            if value is None:
                return subprocess.CompletedProcess(args, 2, "", "")
            return subprocess.CompletedProcess(args, 0, f"{value}\n", "")
        if args[0] == "wipefs":
            output = "\n".join(self.signatures)
            return subprocess.CompletedProcess(args, 0, output, "")
        if args[0] == "mkfs.ext4":
            self.filesystem = "ext4"
            self.label = "APWORKSPACE"
            self.signatures = ["ext4"]
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args}")


@pytest.mark.parametrize("size", [0, 1, CHUNK_BYTES, SIZE])
def test_zeros_are_blank(tmp_path: Path, size: int) -> None:
    device = tmp_path / "device"
    device.write_bytes(bytes(size))

    assert first_nonzero_offset(device, chunk_bytes=CHUNK_BYTES) is None


@pytest.mark.parametrize("offset", [0, CHUNK_BYTES - 1, CHUNK_BYTES, 2 * CHUNK_BYTES + 5, SIZE - 1])
def test_reports_the_first_non_zero_byte(tmp_path: Path, offset: int) -> None:
    contents = bytearray(SIZE)
    contents[offset:] = b"\1" * (SIZE - offset)
    device = tmp_path / "device"
    device.write_bytes(contents)

    assert first_nonzero_offset(device, chunk_bytes=CHUNK_BYTES) == offset


def test_first_use_formats_blank_disk_and_next_boot_accepts_same_filesystem(tmp_path: Path) -> None:
    device = tmp_path / "device"
    device.write_bytes(bytes(SIZE))
    state = FakeDisk()

    assert prepare_workspace_disk(
        device, run_command=state.run, find_nonzero=lambda path: first_nonzero_offset(path, chunk_bytes=CHUNK_BYTES)
    )
    assert not prepare_workspace_disk(device, run_command=state.run)

    assert [command[0] for command in state.commands].count("mkfs.ext4") == 1


@pytest.mark.parametrize(
    ("filesystem", "label", "signatures"),
    [("xfs", "APWORKSPACE", ["xfs"]), ("ext4", "unexpected", ["ext4"]), ("ext4", "APWORKSPACE", ["ext4", "gpt"])],
)
def test_refuses_incompatible_filesystem_without_formatting(
    tmp_path: Path, filesystem: str, label: str, signatures: list[str]
) -> None:
    device = tmp_path / "device"
    device.write_bytes(bytes(SIZE))
    state = FakeDisk(filesystem, label)
    state.signatures = signatures

    with pytest.raises(WorkspaceDiskError, match="Refusing unexpected filesystem"):
        prepare_workspace_disk(device, run_command=state.run)

    assert not any(command[0] == "mkfs.ext4" for command in state.commands)


def test_refuses_unknown_nonzero_disk_without_formatting(tmp_path: Path) -> None:
    device = tmp_path / "device"
    contents = bytearray(SIZE)
    contents[-1] = 1
    device.write_bytes(contents)
    state = FakeDisk()

    with pytest.raises(WorkspaceDiskError, match="non-zero workspace disk"):
        prepare_workspace_disk(
            device, run_command=state.run, find_nonzero=lambda path: first_nonzero_offset(path, chunk_bytes=CHUNK_BYTES)
        )

    assert not any(command[0] == "mkfs.ext4" for command in state.commands)


def test_refuses_unknown_disk_signature_without_formatting(tmp_path: Path) -> None:
    device = tmp_path / "device"
    device.write_bytes(bytes(SIZE))
    state = FakeDisk()
    state.signatures = ["gpt"]

    with pytest.raises(WorkspaceDiskError, match="unrecognized disk signatures"):
        prepare_workspace_disk(device, run_command=state.run)

    assert not any(command[0] == "mkfs.ext4" for command in state.commands)


if __name__ == "__main__":
    pytest_bazel.main()
