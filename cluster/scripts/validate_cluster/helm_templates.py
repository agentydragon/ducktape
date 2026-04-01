"""Validate Helm templates can render without errors.

Finds Cilium values files via runfiles and runs helm template dry-run.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

_CILIUM_VALUES_RLOCATIONS = ["_main/cluster/terraform/main/cilium-values.yaml"]


def _helm_bin() -> Path:
    return get_required_path("multitool/tools/helm/helm")


def _get_values_files() -> list[Path]:
    """Get cilium values files from runfiles."""
    return [get_required_path(rlocation) for rlocation in _CILIUM_VALUES_RLOCATIONS]


async def _exec(*args: str | Path) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    assert proc.returncode is not None
    return proc.returncode, stdout, stderr


async def validate_helm_template(values_file: Path) -> tuple[bool, str]:
    """Validate a Helm chart can render with the given values file."""
    rc, _, stderr = await _exec(
        _helm_bin(), "template", "test-release", "cilium/cilium", "-f", values_file, "--dry-run"
    )
    if rc == 0:
        return True, ""
    return False, stderr.decode().strip()


async def ensure_cilium_repo() -> bool:
    """Ensure the Cilium Helm repo is added."""
    rc, stdout, _ = await _exec(_helm_bin(), "repo", "list", "-o", "json")
    if rc == 0 and "cilium" in stdout.decode():
        return True

    rc, _, stderr = await _exec(_helm_bin(), "repo", "add", "cilium", "https://helm.cilium.io/")
    if rc != 0:
        logger.warning("Failed to add Cilium Helm repo: %s", stderr.decode())
        return False

    rc, _, stderr = await _exec(_helm_bin(), "repo", "update")
    if rc != 0:
        logger.warning("Failed to update Helm repos: %s", stderr.decode())

    return True


async def validate_helm_templates() -> list[str]:
    """Validate all Cilium Helm templates. Returns list of error strings."""
    values_files = _get_values_files()
    if not values_files:
        return []

    if not await ensure_cilium_repo():
        return ["Failed to add Cilium Helm repo"]

    results = await asyncio.gather(*[validate_helm_template(vf) for vf in values_files])
    return [
        f"Helm template failed for {vf}: {error}"
        for vf, (success, error) in zip(values_files, results, strict=True)
        if not success
    ]
