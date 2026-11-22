"""Check the Authentik JWT roster for conflicting outputs and exercise its SOPS rules."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.cdk8s.authentik.jwt_roster import ROTATIONS

_SOPS_CONFIG = "ducktape/.sops.yaml"


def test_names_are_unique() -> None:
    """`name` keys the commit message and the logs; duplicates make both ambiguous."""
    names = [r.name for r in ROTATIONS.rotations]
    assert sorted(names) == sorted(set(names))


def test_output_paths_are_unique() -> None:
    """A rotation must never overwrite another output, including a published Secret."""
    paths = [path for rotation in ROTATIONS.rotations for path in rotation.output_paths]
    assert sorted(paths) == sorted(set(paths))


def test_published_secrets_are_unique() -> None:
    """Two rotations publishing to one namespace/name would overwrite each other."""
    targets = [(r.k8s_secret.namespace, r.k8s_secret.name) for r in ROTATIONS.rotations if r.k8s_secret]
    assert sorted(targets) == sorted(set(targets))


def test_rotation_outputs_encrypt_tokens(runfiles: Runfiles, tmp_path: Path) -> None:
    """Exercise first-match rule selection and field encryption at each actual output path."""
    shutil.copyfile(runfiles.root() / _SOPS_CONFIG, tmp_path / ".sops.yaml")
    sops = runfiles.Rlocation(os.environ["SOPS_RLOCATION"])
    assert sops

    def encrypt(relative_path: Path, document: dict[str, Any]) -> dict[str, Any]:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document))
        result = subprocess.run([sops, "encrypt", relative_path], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
        encrypted = yaml.safe_load(result.stdout)
        assert isinstance(encrypted, dict), relative_path
        return encrypted

    for rotation in ROTATIONS.rotations:
        state = encrypt(
            rotation.sops_file, {rotation.token_field: "sample-token", "expires_unencrypted": "sample-expiry"}
        )
        assert state[rotation.token_field].startswith("ENC["), rotation.sops_file
        assert state["expires_unencrypted"] == "sample-expiry", rotation.sops_file
        if secret := rotation.k8s_secret:
            manifest = encrypt(
                secret.path, {"metadata": {"name": secret.name}, "stringData": {secret.token_key: "sample-token"}}
            )
            assert manifest["stringData"][secret.token_key].startswith("ENC["), secret.path
            assert manifest["metadata"]["name"] == secret.name, secret.path


if __name__ == "__main__":
    pytest_bazel.main()
