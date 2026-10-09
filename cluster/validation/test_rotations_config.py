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

from cluster.cdk8s.authentik.jwt_rotation import ROTATIONS
from util.bazel.runfiles import get_required_path

_SOPS_CONFIG = "_main/.sops.yaml"


def test_names_are_unique() -> None:
    """`name` keys the commit message and the logs; duplicates make both ambiguous."""
    names = [r.name for r in ROTATIONS.rotations]
    assert sorted(names) == sorted(set(names))


def test_sops_files_are_unique() -> None:
    """Two rotations sharing a sops_file silently couple their schedules.

    `remaining_hours()` reads that file's `expires_unencrypted` stamp to decide
    whether a mint is due, so a shared file makes whichever entry ran last
    suppress the other's rotation -- and the suppressed token then expires with
    nothing failing until it does.
    """
    paths = [r.sops_file for r in ROTATIONS.rotations]
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
    config = yaml.safe_load(get_required_path(_SOPS_CONFIG).read_text())
    patterns = [re.compile(rule["path_regex"]) for rule in config["creation_rules"]]

    paths = [str(r.sops_file) for r in ROTATIONS.rotations]
    paths += [str(r.k8s_secret.path) for r in ROTATIONS.rotations if r.k8s_secret]
    uncovered = [p for p in paths if not any(pat.search(p) for pat in patterns)]
    assert not uncovered, f"no .sops.yaml creation_rule matches: {uncovered}"


if __name__ == "__main__":
    pytest_bazel.main()
