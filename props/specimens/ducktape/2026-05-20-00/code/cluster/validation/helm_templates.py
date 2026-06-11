"""Validate Helm templates can render without errors.

Finds Cilium values files via runfiles and runs helm template dry-run.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from cluster.validation.tool_resolve import resolve_tool

logger = logging.getLogger(__name__)

_CILIUM_VALUES_RLOCATIONS = ["_main/cluster/terraform/main/cilium-values.yaml"]


def _helm_bin() -> Path:
    return resolve_tool("helm", "multitool/tools/helm/helm")


def _get_values_files() -> list[Path]:
    """Get cilium values files from runfiles or source tree."""
    try:
        from util.bazel.runfiles import get_required_path  # noqa: PLC0415 — not available outside Bazel

        return [get_required_path(rloc) for rloc in _CILIUM_VALUES_RLOCATIONS]
    except (ImportError, RuntimeError):
        # Outside Bazel: resolve relative to repo root (strip _main/ prefix)
        repo_root = Path(__file__).resolve().parents[2]
        return [repo_root / rloc.removeprefix("_main/") for rloc in _CILIUM_VALUES_RLOCATIONS]


async def _exec(*args: str | Path) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    return await proc.wait(), stdout, stderr


class HelmTemplateError(Exception):
    """Raised when helm template rendering fails."""


async def _validate_helm_template(values_file: Path) -> None:
    """Validate a Helm chart can render with the given values file."""
    rc, _, stderr = await _exec(
        _helm_bin(), "template", "test-release", "cilium/cilium", "-f", values_file, "--dry-run"
    )
    if rc != 0:
        raise HelmTemplateError(f"Helm template failed for {values_file}: {stderr.decode().strip()}")


async def _ensure_cilium_repo() -> None:
    """Ensure the Cilium Helm repo is added. Raises on failure."""
    rc, stdout, _ = await _exec(_helm_bin(), "repo", "list", "-o", "json")
    if rc == 0 and "cilium" in stdout.decode():
        return

    rc, _, stderr = await _exec(_helm_bin(), "repo", "add", "cilium", "https://helm.cilium.io/")
    if rc != 0:
        raise RuntimeError(f"Failed to add Cilium Helm repo: {stderr.decode()}")

    rc, _, stderr = await _exec(_helm_bin(), "repo", "update")
    if rc != 0:
        raise RuntimeError(f"Failed to update Helm repos: {stderr.decode()}")


async def validate_helm_templates() -> list[str]:
    """Validate all Cilium Helm templates. Returns list of error strings."""
    values_files = _get_values_files()
    if not values_files:
        return []

    await _ensure_cilium_repo()

    results = await asyncio.gather(*[_validate_helm_template(vf) for vf in values_files], return_exceptions=True)
    return [str(e) for e in results if isinstance(e, HelmTemplateError)]
