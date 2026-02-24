"""Validate all SealedSecrets can be decrypted with OpenTofu keypair.

Uses kubeseal --recovery-unseal (works offline, no cluster needed).

Run via Bazel: bazel run //cluster/scripts/validate_cluster:validate_sealed_secrets

TODO: Make this a separate pre-commit hook with trigger pattern ``*sealed*.yaml``
under ``cluster/k8s/`` and ``cluster/terraform/bootstrap/persistent-auth/``.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from cluster.scripts.validate_cluster.cluster import _K8S_SUBPATH
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory


def get_private_key_from_tofu(tf_dir: Path) -> str | None:
    """Extract sealed_secrets_private_key_pem from tofu state."""
    state_file = tf_dir / "terraform.tfstate"
    if not state_file.exists():
        return None

    tofu_bin = get_required_path("multitool/tools/tofu/tofu")
    result = subprocess.run(
        [tofu_bin, "output", "-raw", "sealed_secrets_private_key_pem"],
        check=False,
        cwd=tf_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not read sealed_secrets_private_key_pem from tofu state: {result.stderr}")
    return result.stdout


def find_sealed_secrets(k8s_dir: Path) -> list[Path]:
    """Find all SealedSecret YAML files."""
    sealed_secrets = []
    for yaml_file in k8s_dir.rglob("*sealed*.yaml"):
        content = yaml_file.read_text()
        if "kind: SealedSecret" in content:
            sealed_secrets.append(yaml_file)
    return sealed_secrets


def validate_sealed_secret(sealed_secret_path: Path, private_key_path: Path) -> tuple[bool, str]:
    """Validate a single SealedSecret can be decrypted."""
    kubeseal_bin = get_required_path("multitool/tools/kubeseal/kubeseal")
    result = subprocess.run(
        [kubeseal_bin, "--recovery-unseal", "--recovery-private-key", private_key_path],
        check=False,
        stdin=sealed_secret_path.open("rb"),
        capture_output=True,
    )
    if result.returncode == 0:
        return True, ""
    return False, result.stderr.decode().strip()


def main() -> int:
    workspace = get_build_workspace_directory()
    cluster_root = workspace / "cluster"
    tf_dir = cluster_root / "terraform" / "bootstrap" / "persistent-auth"
    k8s_dir = workspace / _K8S_SUBPATH

    if not (tf_dir / "terraform.tfstate").exists():
        print(f"⚠️  No tofu state found at {tf_dir}/terraform.tfstate")
        print("   Skipping SealedSecret validation (state not initialized)")
        return 0

    private_key = get_private_key_from_tofu(tf_dir)
    if not private_key:
        print("⚠️  Could not read private key from tofu state")
        print(f"   Run 'tofu apply' in {tf_dir} first")
        return 1

    sealed_secrets = find_sealed_secrets(k8s_dir)
    if not sealed_secrets:
        print("✅ No SealedSecret files found")
        return 0

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(private_key)
        private_key_path = Path(f.name)

    try:
        failed = 0
        for sealed_secret in sealed_secrets:
            success, error = validate_sealed_secret(sealed_secret, private_key_path)
            if success:
                print(f"✅ {sealed_secret}")
            else:
                print(f"❌ {sealed_secret}")
                print(f"   Error: {error}")
                failed += 1

        if failed > 0:
            print()
            print("ERROR: Some SealedSecrets cannot be decrypted with the tofu keypair")
            print(f"Run 'cd {tf_dir} && tofu apply' to re-seal")
            return 1

        print()
        print(f"✅ All {len(sealed_secrets)} SealedSecrets validated successfully")
        return 0

    finally:
        private_key_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
