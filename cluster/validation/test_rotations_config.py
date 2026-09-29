"""The shipped rotations.yaml parses, and its entries stay independent.

`//cluster/rotators/authentik_jwt_rotation:test_rotate` exercises the model
against hand-built dicts; nothing looked at the file the CronJob actually runs.
A malformed entry therefore passed CI and failed hourly in the cluster instead,
which is the wrong place to find out.

Not a change detector: nothing here restates a value from the manifest. The
claims are that every entry satisfies the model, and that three invariants the
model cannot express -- they are cross-entry -- hold.
"""

from __future__ import annotations

import re

import pytest
import pytest_bazel
import yaml

from cluster.rotators.authentik_jwt_rotation.rotate import Rotation
from util.bazel.runfiles import get_required_path

_ROTATIONS = "_main/cluster/k8s/agents/authentik-jwt-rotation/rotations.yaml"
_SOPS_CONFIG = "_main/.sops.yaml"


@pytest.fixture(scope="module")
def rotations() -> list[Rotation]:
    raw = yaml.safe_load(get_required_path(_ROTATIONS).read_text())
    return [Rotation.model_validate(entry) for entry in raw["rotations"]]


def test_every_entry_parses(rotations: list[Rotation]) -> None:
    assert rotations, "rotations.yaml declares no rotations"


def test_names_are_unique(rotations: list[Rotation]) -> None:
    """`name` keys the commit message and the logs; duplicates make both ambiguous."""
    names = [r.name for r in rotations]
    assert sorted(names) == sorted(set(names))


def test_sops_files_are_unique(rotations: list[Rotation]) -> None:
    """Two rotations sharing a sops_file silently couple their schedules.

    `remaining_hours()` reads that file's `expires_unencrypted` stamp to decide
    whether a mint is due, so a shared file makes whichever entry ran last
    suppress the other's rotation -- and the suppressed token then expires with
    nothing failing until it does.
    """
    paths = [r.sops_file for r in rotations]
    assert sorted(paths) == sorted(set(paths))


def test_published_secrets_are_unique(rotations: list[Rotation]) -> None:
    """Two rotations publishing to one namespace/name would overwrite each other."""
    targets = [(r.k8s_secret.namespace, r.k8s_secret.name) for r in rotations if r.k8s_secret]
    assert sorted(targets) == sorted(set(targets))


def test_sops_files_have_creation_rules(rotations: list[Rotation]) -> None:
    """A state file matching no creation_rule cannot be written at all.

    `sops --encrypt` refuses a path it has no recipients for, so the omission
    surfaces as a failed hourly CronJob rather than as anything in CI. The
    published k8s Secret paths are covered by the generic cluster deployment-secrets rule
    and are checked here for the same reason.
    """
    config = yaml.safe_load(get_required_path(_SOPS_CONFIG).read_text())
    patterns = [re.compile(rule["path_regex"]) for rule in config["creation_rules"]]

    paths = [str(r.sops_file) for r in rotations]
    paths += [str(r.k8s_secret.path) for r in rotations if r.k8s_secret]
    uncovered = [p for p in paths if not any(pat.search(p) for pat in patterns)]
    assert not uncovered, f"no .sops.yaml creation_rule matches: {uncovered}"


if __name__ == "__main__":
    pytest_bazel.main()
