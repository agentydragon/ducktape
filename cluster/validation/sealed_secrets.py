"""Validate all SealedSecrets can be decrypted with OpenTofu keypair.

Uses kubeseal --recovery-unseal (works offline, no cluster needed).
Called directly by //devinfra/precommit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

from cluster.validation.cluster import _K8S_SUBPATH
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory

logger = logging.getLogger(__name__)

_TF_ROOT = "cluster/terraform/main"


async def get_private_key_from_tofu(tf_dir: Path) -> str | None:
    """Extract sealed_secrets private key from tofu state (PG backend)."""
    tofu_bin = get_required_path("multitool/tools/tofu/tofu")
    proc = await asyncio.create_subprocess_exec(
        tofu_bin, "state", "pull", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=tf_dir
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.debug("tofu state pull failed (expected if no PG access): %s", stderr.decode().strip())
        return None

    state = json.loads(stdout)
    for resource in state.get("resources", []):
        if resource.get("type") == "tls_private_key" and resource.get("name") == "sealed_secrets":
            for instance in resource.get("instances", []):
                if key := instance.get("attributes", {}).get("private_key_pem"):
                    return str(key)
    return None


def find_sealed_secrets(k8s_dir: Path) -> list[Path]:
    """Find all SealedSecret YAML files."""
    sealed_secrets = []
    for yaml_file in k8s_dir.rglob("*sealed*.yaml"):
        content = yaml_file.read_text()
        if "kind: SealedSecret" in content:
            sealed_secrets.append(yaml_file)
    return sealed_secrets


async def validate_sealed_secret(sealed_secret_path: Path, private_key_path: Path) -> tuple[bool, str]:
    """Validate a single SealedSecret can be decrypted."""
    kubeseal_bin = get_required_path("multitool/tools/kubeseal/kubeseal")
    with sealed_secret_path.open("rb") as stdin_file:
        proc = await asyncio.create_subprocess_exec(
            kubeseal_bin,
            "--recovery-unseal",
            "--recovery-private-key",
            str(private_key_path),
            stdin=stdin_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    if proc.returncode == 0:
        return True, ""
    return False, stderr.decode().strip()


async def validate_all() -> list[str]:
    """Validate all SealedSecrets, returning a list of error strings (empty = success)."""
    workspace = get_build_workspace_directory()
    tf_dir = workspace / _TF_ROOT
    k8s_dir = workspace / _K8S_SUBPATH

    if not tf_dir.exists():
        return []

    private_key = await get_private_key_from_tofu(tf_dir)
    if private_key is None:
        return []

    if not (sealed_secrets := find_sealed_secrets(k8s_dir)):
        return []

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(private_key)
        private_key_path = Path(f.name)

    try:
        results = await asyncio.gather(*[validate_sealed_secret(ss, private_key_path) for ss in sealed_secrets])
        return [
            f"FAIL {ss}: {error}" for (success, error), ss in zip(results, sealed_secrets, strict=True) if not success
        ]
    finally:
        await asyncio.to_thread(private_key_path.unlink, True)
