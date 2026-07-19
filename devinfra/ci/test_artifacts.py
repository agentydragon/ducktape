import pytest_bazel

from devinfra.ci.artifacts import ARTIFACTS


def test_artifacts_are_derived_from_release_metadata() -> None:
    artifacts = {artifact.pkg: artifact for artifact in ARTIFACTS}

    assert artifacts["hostexecd"].filename == "hostexecd"
    assert artifacts["hostexecd"].release_tag_prefix == "hostexecd"
    assert artifacts["aiquota-extension"].filename == "aiquota.zip"
    assert artifacts["aiquota-extension"].release_tag_prefix == "aiquota"
    assert "skill-buildbuddy_api" in artifacts


if __name__ == "__main__":
    pytest_bazel.main()
