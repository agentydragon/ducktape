"""The authentik-jwt-rotation roster's entries stay independent.

The generator builds the roster (`cluster/cdk8s/authentik/jwt_rotation.py`'s `ROTATIONS`) from
the rotator's own `Config`, so every entry satisfies the model by construction. What the model
cannot express is cross-entry, and a violation would pass CI and fail hourly in the cluster
instead, which is the wrong place to find out.

Not a change detector: nothing here restates a value from the roster.
"""

from __future__ import annotations

import re

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.cdk8s.authentik.jwt_rotation import ROTATIONS

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


def test_sops_files_have_creation_rules() -> None:
    """A state file matching no creation_rule cannot be written at all.

    `sops --encrypt` refuses a path it has no recipients for, so the omission
    surfaces as a failed hourly CronJob rather than as anything in CI. The
    published k8s Secret paths are covered by the generic cluster deployment-secrets rule
    and are checked here for the same reason.
    """
    config = yaml.safe_load((Runfiles.CreateOrRaise().root() / _SOPS_CONFIG).read_text())
    patterns = [re.compile(rule["path_regex"]) for rule in config["creation_rules"]]

    paths = [str(path) for rotation in ROTATIONS.rotations for path in rotation.output_paths]
    uncovered = [p for p in paths if not any(pat.search(p) for pat in patterns)]
    assert not uncovered, f"no .sops.yaml creation_rule matches: {uncovered}"


if __name__ == "__main__":
    pytest_bazel.main()
