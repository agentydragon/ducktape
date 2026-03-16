"""Validate Helm templates can render without errors."""

from __future__ import annotations

import subprocess
from pathlib import Path

from util.bazel.runfiles import get_required_path


def _helm_bin() -> Path:
    return get_required_path("multitool/tools/helm/helm")


def validate_helm_template(values_file: Path) -> None:
    """Validate a Helm chart can render with the given values file.

    Raises subprocess.CalledProcessError on failure.
    """
    subprocess.run(
        [_helm_bin(), "template", "test-release", "cilium/cilium", "-f", values_file, "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )


def ensure_cilium_repo() -> None:
    """Ensure the Cilium Helm repo is added. Raises on failure."""
    result = subprocess.run([_helm_bin(), "repo", "list", "-o", "json"], check=False, capture_output=True, text=True)
    if result.returncode == 0 and "cilium" in result.stdout:
        return

    subprocess.run(
        [_helm_bin(), "repo", "add", "cilium", "https://helm.cilium.io/"], check=True, capture_output=True, text=True
    )
    subprocess.run([_helm_bin(), "repo", "update"], check=True, capture_output=True, text=True)
