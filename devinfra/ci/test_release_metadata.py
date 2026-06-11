import json
from pathlib import Path

import pytest_bazel

from devinfra.ci.release_metadata import write_release_metadata


def test_write_release_metadata(tmp_path: Path) -> None:
    output = tmp_path / "debundle.release.json"

    write_release_metadata(
        output=output,
        package="debundle",
        tag="debundle-0123456789ab",
        git_commit="abcd" * 10,
        binary="debundle",
        sha256="1234",
        platform="linux-amd64",
    )

    assert json.loads(output.read_text()) == {
        "binary": "debundle",
        "git_commit": "abcd" * 10,
        "package": "debundle",
        "platform": "linux-amd64",
        "sha256": "1234",
        "tag": "debundle-0123456789ab",
    }


def test_write_release_metadata_omits_empty_platform(tmp_path: Path) -> None:
    output = tmp_path / "artifact.release.json"

    write_release_metadata(
        output=output,
        package="artifact",
        tag="artifact-0123456789ab",
        git_commit="abcd" * 10,
        binary="artifact",
        sha256="1234",
        platform=None,
    )

    assert "platform" not in json.loads(output.read_text())


if __name__ == "__main__":
    pytest_bazel.main()
