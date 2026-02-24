"""Validate Helm templates can render without errors.

Finds Cilium values files via runfiles and runs helm template dry-run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from util.bazel.runfiles import get_required_path

_CILIUM_VALUES_RLOCATIONS = ["_main/cluster/terraform/bootstrap/infrastructure/cilium-values.yaml"]


def _helm_bin() -> Path:
    return get_required_path("multitool/tools/helm/helm")


def _get_values_files() -> list[Path]:
    """Get cilium values files from runfiles."""
    return [get_required_path(rlocation) for rlocation in _CILIUM_VALUES_RLOCATIONS]


def validate_helm_template(values_file: Path) -> tuple[bool, str]:
    """Validate a Helm chart can render with the given values file."""
    result = subprocess.run(
        [_helm_bin(), "template", "test-release", "cilium/cilium", "-f", values_file, "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, ""
    return False, result.stderr.strip()


def ensure_cilium_repo() -> bool:
    """Ensure the Cilium Helm repo is added."""
    result = subprocess.run([_helm_bin(), "repo", "list", "-o", "json"], check=False, capture_output=True, text=True)
    if result.returncode == 0 and "cilium" in result.stdout:
        return True

    result = subprocess.run(
        [_helm_bin(), "repo", "add", "cilium", "https://helm.cilium.io/"], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"❌ Failed to add Cilium Helm repo: {result.stderr}")
        return False

    result = subprocess.run([_helm_bin(), "repo", "update"], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"⚠️  Failed to update Helm repos: {result.stderr}")

    return True


def validate_helm_templates() -> list[str]:
    """Validate all Cilium Helm templates. Returns list of error strings."""
    values_files = _get_values_files()
    if not values_files:
        return []

    if not ensure_cilium_repo():
        return ["Failed to add Cilium Helm repo"]

    errors = []
    for values_file in values_files:
        success, error = validate_helm_template(values_file)
        if not success:
            errors.append(f"Helm template failed for {values_file}: {error}")

    return errors
