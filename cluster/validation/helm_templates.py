"""Validate Helm templates can render without errors.

Finds the Cilium values file via runfiles and runs helm template dry-run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from python.runfiles import Runfiles

_CILIUM_VALUES_RLOCATION = "ducktape/cluster/terraform/main/cilium-values.yaml"


async def _exec(*args: str | Path) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    return await proc.wait(), stdout, stderr


class HelmTemplateError(Exception):
    """Raised when helm template rendering fails."""


async def _validate_helm_template(helm: Path, values_file: Path) -> None:
    """Validate a Helm chart can render with the given values file."""
    rc, _, stderr = await _exec(helm, "template", "test-release", "cilium/cilium", "-f", values_file, "--dry-run")
    if rc != 0:
        raise HelmTemplateError(f"Helm template failed for {values_file}: {stderr.decode().strip()}")


async def _ensure_cilium_repo(helm: Path) -> None:
    """Ensure the Cilium Helm repo is added. Raises on failure."""
    rc, stdout, _ = await _exec(helm, "repo", "list", "-o", "json")
    if rc == 0 and "cilium" in stdout.decode():
        return

    rc, _, stderr = await _exec(helm, "repo", "add", "cilium", "https://helm.cilium.io/")
    if rc != 0:
        raise RuntimeError(f"Failed to add Cilium Helm repo: {stderr.decode()}")

    rc, _, stderr = await _exec(helm, "repo", "update")
    if rc != 0:
        raise RuntimeError(f"Failed to update Helm repos: {stderr.decode()}")


async def validate_helm_templates() -> list[str]:
    """Validate the Cilium Helm template. Returns list of error strings."""
    root = Runfiles.CreateOrRaise().root()
    helm = root / "multitool/tools/helm/helm"
    await _ensure_cilium_repo(helm)
    try:
        await _validate_helm_template(helm, root / _CILIUM_VALUES_RLOCATION)
    except HelmTemplateError as exc:
        return [str(exc)]
    return []
