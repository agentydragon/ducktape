from pathlib import Path

import pytest
import pytest_bazel

from agentplane.runner.assert_blank_device import first_nonzero_offset

# Small enough that the cases cross chunk boundaries and end in a short final chunk.
CHUNK_BYTES = 4096
SIZE = 3 * CHUNK_BYTES + 17


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


if __name__ == "__main__":
    pytest_bazel.main()
