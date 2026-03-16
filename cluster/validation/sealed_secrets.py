"""Validate all SealedSecrets can be decrypted with OpenTofu keypair.

Uses kubeseal --recovery-unseal (works offline, no cluster needed).
Called directly by //devinfra/precommit.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from cluster.validation.cluster import _K8S_SUBPATH
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory


def get_private_key_from_tofu(state_path: Path) -> str:
    """Extract sealed_secrets_private_key_pem from tofu state."""
    tofu_bin = get_required_path("multitool/tools/tofu/tofu")
    return subprocess.run(
        [tofu_bin, "output", "-raw", "-state", state_path, "sealed_secrets_private_key_pem"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


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
    state_path = workspace / "cluster" / "terraform" / "bootstrap" / "persistent-auth" / "terraform.tfstate"
    k8s_dir = workspace / _K8S_SUBPATH

    if not state_path.exists():
        print(f"No tofu state at {state_path} — skipping SealedSecret validation")
        return 0

    private_key = get_private_key_from_tofu(state_path)

    if not (sealed_secrets := find_sealed_secrets(k8s_dir)):
        print("No SealedSecret files found")
        return 0

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(private_key)
        private_key_path = Path(f.name)

    try:
        failed = 0
        for sealed_secret in sealed_secrets:
            success, error = validate_sealed_secret(sealed_secret, private_key_path)
            if success:
                print(f"OK {sealed_secret}")
            else:
                print(f"FAIL {sealed_secret}")
                print(f"   Error: {error}")
                failed += 1

        if failed > 0:
            print()
            print("ERROR: Some SealedSecrets cannot be decrypted with the tofu keypair")
            print(f"Run 'cd {state_path.parent} && tofu apply' to re-seal")
            return 1

        print()
        print(f"All {len(sealed_secrets)} SealedSecrets validated successfully")
        return 0

    finally:
        private_key_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
