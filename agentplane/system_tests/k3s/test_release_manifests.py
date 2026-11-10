"""Check safe YAML parsing against the release artifacts packaged for the k3s stack."""

import os
from pathlib import Path

import pytest
import yaml
from python.runfiles import Runfiles

from agentplane.system_tests.k3s.release_manifests import load_release_manifest

_PINNED_MANIFESTS = (
    "CERT_MANAGER_INSTALL_RLOCATION",
    "TRUST_MANAGER_INSTALL_RLOCATION",
    "KUBEVIRT_OPERATOR_RLOCATION",
    "KYVERNO_INSTALL_RLOCATION",
)


@pytest.mark.parametrize("variable", _PINNED_MANIFESTS)
def test_release_manifest_matches_safe_loader(runfiles: Runfiles, variable: str) -> None:
    path = runfiles.root() / os.environ[variable]
    expected = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]

    assert load_release_manifest(path, variable) == expected


def test_release_manifest_rejects_python_object_tags(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.yaml"
    path.write_text("!!python/object:builtins.object {}\n", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        load_release_manifest(path, "unsafe-test")


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
