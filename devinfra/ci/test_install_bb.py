import base64
import hashlib
import json
import stat
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.install_bb import install_bb


def write_pin(tmp_path: Path, contents: bytes) -> tuple[Path, Path]:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(contents)
    pins = tmp_path / "artifact-pins.json"
    pins.write_text(
        json.dumps(
            {
                "pins": {
                    "bb": {
                        "url": artifact.as_uri(),
                        "sha256": base64.b64encode(hashlib.sha256(contents).digest()).decode(),
                    }
                }
            }
        )
    )
    return pins, artifact


def test_verified_download_is_installed_executable(tmp_path: Path) -> None:
    contents = b"verified CLI bytes"
    pins, _ = write_pin(tmp_path, contents)
    destination = tmp_path / "bin" / "bb"

    install_bb(pins, destination)

    assert destination.read_bytes() == contents
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("already_installed", [False, True])
def test_corrupt_download_does_not_replace_destination(tmp_path: Path, already_installed: bool) -> None:
    pins, artifact = write_pin(tmp_path, b"expected bytes")
    artifact.write_bytes(b"corrupt download")
    destination = tmp_path / "bin" / "bb"
    if already_installed:
        destination.parent.mkdir()
        destination.write_bytes(b"previous CLI")

    with pytest.raises(ValueError, match="pinned SHA-256"):
        install_bb(pins, destination)

    if already_installed:
        assert destination.read_bytes() == b"previous CLI"
        assert list(destination.parent.iterdir()) == [destination]
    else:
        assert list(destination.parent.iterdir()) == []


if __name__ == "__main__":
    pytest_bazel.main()
