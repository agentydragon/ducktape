import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci.install_clis import install_clis


@pytest.fixture
def pins(tmp_path: Path) -> Path:
    entries = {}
    for name in ("bb", "bbapi"):
        asset = tmp_path / name
        asset.write_bytes(name.encode())
        entries[name] = {
            "url": asset.as_uri(),
            "sha256": base64.b64encode(hashlib.sha256(asset.read_bytes()).digest()).decode(),
        }
    pin_file = tmp_path / "artifact-pins.json"
    pin_file.write_text(json.dumps({"pins": entries}))
    return pin_file


def test_install_verified_executables(pins: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"

    install_clis(pins, bin_dir)

    assert {path.name: path.read_bytes() for path in bin_dir.iterdir()} == {"bb": b"bb", "bbapi": b"bbapi"}
    assert all(os.access(path, os.X_OK) for path in bin_dir.iterdir())


def test_reject_corrupt_second_download_before_replacing_any_cli(pins: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bb").write_bytes(b"previous bb")
    (tmp_path / "bbapi").write_bytes(b"corrupt download")

    with pytest.raises(ValueError, match="bbapi download does not match the pinned SHA-256"):
        install_clis(pins, bin_dir)

    assert {path.name: path.read_bytes() for path in bin_dir.iterdir()} == {"bb": b"previous bb"}


if __name__ == "__main__":
    pytest_bazel.main()
