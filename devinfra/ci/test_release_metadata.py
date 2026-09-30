import hashlib
import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.release_metadata import write_release_metadata


def asset(directory: Path, name: str, content: bytes) -> Path:
    path = directory / name
    path.write_bytes(content)
    return path


def test_write_release_metadata(tmp_path: Path) -> None:
    output = tmp_path / "debundle.release.json"

    write_release_metadata(
        output=output,
        package="debundle",
        tag="debundle-0123456789ab",
        git_commit="abcd" * 10,
        assets=[asset(tmp_path, "debundle", b"binary")],
        platform="linux-amd64",
    )

    assert json.loads(output.read_text()) == {
        "binary": "debundle",
        "git_commit": "abcd" * 10,
        "package": "debundle",
        "platform": "linux-amd64",
        "sha256": hashlib.sha256(b"binary").hexdigest(),
        "tag": "debundle-0123456789ab",
    }


def test_write_release_metadata_omits_empty_platform(tmp_path: Path) -> None:
    output = tmp_path / "artifact.release.json"

    write_release_metadata(
        output=output,
        package="artifact",
        tag="artifact-0123456789ab",
        git_commit="abcd" * 10,
        assets=[asset(tmp_path, "artifact", b"binary")],
        platform=None,
    )

    assert "platform" not in json.loads(output.read_text())


def test_other_assets_are_listed_as_sidecars_by_file_name(tmp_path: Path) -> None:
    """The sidecar's hash lets a consumer pin it next to the binary (Gaffer's `http_file`)."""
    output = tmp_path / "debundle.release.json"

    write_release_metadata(
        output=output,
        package="debundle",
        tag="debundle-0123456789ab",
        git_commit="abcd" * 10,
        assets=[asset(tmp_path, "selector_cpsat_solver", b"solver"), asset(tmp_path, "debundle", b"binary")],
        platform=None,
    )

    metadata = json.loads(output.read_text())
    assert metadata["binary"] == "debundle"
    assert metadata["sha256"] == hashlib.sha256(b"binary").hexdigest()
    assert metadata["sidecars"] == {"selector_cpsat_solver": hashlib.sha256(b"solver").hexdigest()}


def test_a_release_without_an_asset_named_after_the_package_has_no_binary(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one asset named debundle"):
        write_release_metadata(
            output=tmp_path / "debundle.release.json",
            package="debundle",
            tag="debundle-0123456789ab",
            git_commit="abcd" * 10,
            assets=[asset(tmp_path, "selector_cpsat_solver", b"solver")],
            platform=None,
        )


if __name__ == "__main__":
    pytest_bazel.main()
