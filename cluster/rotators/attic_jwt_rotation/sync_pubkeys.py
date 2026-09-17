"""Regenerate nix/attic-pubkeys.json from cache-keys.sops.yaml.

Local/operator-only: derives each cache's public key from its SOPS-encrypted
private key, so it needs cluster-secret SOPS decrypt access — an operator's age
key, not a machine-level one (see AGENTS.md § SOPS). Deliberately not bundled
into the attic-jwt-rotation container image (see rotate.py's module docstring);
run this by hand after cluster/k8s/nix-cache/cache-keys.sops.yaml changes.
"""

import json
import subprocess
from pathlib import Path
from typing import Annotated

import typer
import yaml

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _decrypted_keypairs(sops_file: Path) -> dict[str, str]:
    """Cache name -> `<name>:<base64>` secret, decrypted from `sops_file`."""
    result = subprocess.run(["sops", "-d", str(sops_file)], capture_output=True, text=True, check=True)
    secret = yaml.safe_load(result.stdout)
    keypairs: dict[str, str] = secret["stringData"]
    return keypairs


def _public_key(secret_keypair: str) -> str:
    """`nix key convert-secret-to-public` on a `<name>:<base64>` secret string."""
    result = subprocess.run(
        ["nix", "key", "convert-secret-to-public"], input=secret_keypair, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@app.command()
def sync(
    sops_file: Annotated[Path, typer.Option(help="SOPS-encrypted Secret carrying the per-cache signing keys")] = Path(
        "cluster/k8s/nix-cache/cache-keys.sops.yaml"
    ),
    pubkeys_file: Annotated[
        Path, typer.Option(help="Output consumed by nix/nixos/modules/attic-substituter.nix and CI")
    ] = Path("nix/attic-pubkeys.json"),
) -> None:
    """Overwrite `pubkeys_file` with public keys derived from `sops_file`, in its key order."""
    keypairs = _decrypted_keypairs(sops_file)
    pubkeys = [_public_key(secret_keypair) for secret_keypair in keypairs.values()]
    pubkeys_file.write_text(json.dumps(pubkeys, indent=2) + "\n")


if __name__ == "__main__":
    app()
