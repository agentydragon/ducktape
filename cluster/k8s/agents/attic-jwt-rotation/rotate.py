"""Rotate Attic JWTs into SOPS-encrypted files.

One run processes every token listed in the YAML config. For each token it:

  1. Reads the unencrypted-by-suffix `expires_unencrypted` field of the existing
     `sops_file` (no SOPS decryption, no in-cluster age key) and skips when
     remaining validity exceeds `rotate_below_hours`. With 1-year validity and a
     24h threshold, a real mint runs only ~once per token per year, but a failed
     rotation self-heals on the next hourly run.
  2. Mints a fresh JWT via `kubectl exec deploy/attic -- atticadm -f
     /config/server.toml make-token …`, so the HS256 signing secret never leaves
     the attic pod. The rotator's ServiceAccount only needs `pods/exec` on the
     attic deployment in the nix-cache namespace.
  3. Writes `expires_unencrypted` (from the JWT's own `exp` claim, so the
     freshness check is authoritative) plus the token, then `sops encrypt`s in
     place.

Everything that actually rotated this cycle lands in a single combined commit.
The whole run operates from the clone root so SOPS creation rules (matched on
repo-relative paths) resolve.
"""

import base64
import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Token(BaseModel):
    name: str = Field(description="Human-readable name for logs and the commit message")
    sops_file: Path = Field(description="Repo-relative path to the encrypted output file")
    sub: str = Field(description="JWT subject claim passed to atticadm --sub")
    validity: str = Field(description="Token lifetime passed to atticadm --validity (e.g. '1 year')")
    pull: list[str] = Field(description="Caches the token may pull from (atticadm --pull, repeated)")
    push: list[str] = Field(
        default_factory=list, description="Caches the token may push to (atticadm --push, repeated); empty for read-only tokens"
    )


class Config(BaseModel):
    tokens: list[Token]
    attic_namespace: str = Field(
        default="nix-cache", description="Namespace of the attic deployment exec'd for minting"
    )
    attic_deployment: str = Field(default="deploy/attic", description="Deployment exec'd for minting")
    server_config: str = Field(
        default="/config/server.toml",
        description="Attic server config path (inside the attic pod) passed to atticadm -f",
    )
    token_field: str = Field(default="attic_token", description="YAML field name under which the JWT is written")
    rotate_below_hours: int = Field(
        default=24, description="Mint a fresh token once remaining validity drops below this"
    )
    github_repo: str = "agentydragon/ducktape"
    sops_config: str = Field(default=".sops.yaml", description="Repo path sops reads to pick the recipient set")
    git_author_name: str = "attic-jwt-rotation"
    git_author_email: str = "noreply@allegedly.works"


# JWT claims are an untyped external payload; we read a handful of well-known keys.
def jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT's base64url payload (no signature verification)."""
    segment = token.split(".")[1]
    padded = segment + "=" * (-len(segment) % 4)
    payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return payload


def unencrypted_stamps(sops_file: Path) -> dict[str, Any]:
    """The plaintext `*_unencrypted` stamps from a sops file, parsed as YAML.

    A sops-encrypted YAML file is still valid YAML, and the `*_unencrypted`-suffixed
    keys keep their plaintext values (SOPS leaves them in clear), so the freshness
    stamp loads straight out without the in-cluster age key. Returns `{}` when the
    file is absent or empty.
    """
    if not sops_file.exists():
        return {}
    return yaml.safe_load(sops_file.read_text()) or {}


def remaining_hours(sops_file: Path) -> float | None:
    """Hours until the existing token expires, or None if absent/unstamped."""
    expires = unencrypted_stamps(sops_file).get("expires_unencrypted")
    if expires is None:
        return None
    return (datetime.fromisoformat(expires) - datetime.now(UTC)).total_seconds() / 3600


def mint_attic_token(token: Token, config: Config) -> str:
    """Mint a JWT via `kubectl exec deploy/attic -- atticadm make-token …`.

    The HS256 signing secret stays in the attic pod; the rotator only needs
    `pods/exec` on the attic deployment.
    """
    args = [
        "kubectl",
        "-n",
        config.attic_namespace,
        "exec",
        config.attic_deployment,
        "--",
        "atticadm",
        "-f",
        config.server_config,
        "make-token",
        "--sub",
        token.sub,
        "--validity",
        token.validity,
    ]
    args += [arg for cache in token.pull for arg in ("--pull", cache)]
    args += [arg for cache in token.push for arg in ("--push", cache)]
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    jwt = result.stdout.strip()
    if not jwt:
        raise RuntimeError(f"{token.name}: atticadm make-token returned empty output\n{result.stderr}")
    return jwt


def rotate_one(token: Token, config: Config) -> bool:
    """Mint + write a fresh JWT for one token. Returns True if it wrote."""
    remaining = remaining_hours(token.sops_file)
    if remaining is not None and remaining > config.rotate_below_hours:
        logger.info("%s: %.0fh remaining > %dh threshold; skipping", token.name, remaining, config.rotate_below_hours)
        return False
    logger.info("%s: rotating (remaining=%s)", token.name, "none" if remaining is None else f"{remaining:.0f}h")

    jwt = mint_attic_token(token, config)
    payload = jwt_payload(jwt)
    if "exp" not in payload:
        raise RuntimeError(f"{token.name}: JWT payload missing exp claim; payload: {payload}")
    expires_iso = datetime.fromtimestamp(int(payload["exp"]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # `*_unencrypted` keys match SOPS's default unencrypted_suffix, so they stay
    # plaintext after `sops encrypt --in-place` — the next run reads them back
    # for the freshness check without decryption.
    stamps = {"expires_unencrypted": expires_iso, config.token_field: jwt}
    token.sops_file.parent.mkdir(parents=True, exist_ok=True)
    token.sops_file.write_text(yaml.safe_dump(stamps, sort_keys=False, width=2**31))
    subprocess.run(["sops", "encrypt", "--in-place", str(token.sops_file)], check=True)
    logger.info("%s: wrote token expiring %s", token.name, expires_iso)
    return True


def sparse_clone(config: Config, github_pat: str) -> None:
    """Init + sparse-fetch only .sops.yaml and the tokens' sops_files into cwd."""
    remote = f"https://x-access-token:{github_pat}@github.com/{config.github_repo}.git"
    sparse_paths = [config.sops_config, *(str(t.sops_file) for t in config.tokens)]
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], check=True)
    subprocess.run(["git", "config", "core.sparseCheckout", "true"], check=True)
    Path(".git/info/sparse-checkout").write_text("\n".join(sparse_paths) + "\n")
    subprocess.run(["git", "fetch", "-q", "--depth=1", "--no-tags", "origin", "devel"], check=True)
    subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], check=True)


def commit_and_push(config: Config, rotated: list[str]) -> None:
    subprocess.run(["git", "config", "user.name", config.git_author_name], check=True)
    subprocess.run(["git", "config", "user.email", config.git_author_email], check=True)
    # Stage exactly the SOPS files for tokens that rotated this cycle (no `git add -A`).
    for token in config.tokens:
        if token.name in rotated:
            subprocess.run(["git", "add", "--", str(token.sops_file)], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        logger.info("tokens minted but SOPS files unchanged on disk (unexpected); nothing to commit")
        return
    message = f"chore: rotate attic JWTs ({datetime.now(UTC):%Y-%m-%d}): {', '.join(rotated)}"
    subprocess.run(["git", "commit", "-q", "-m", message], check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:devel"], check=True)
    logger.info("pushed: %s", ", ".join(rotated))


def main(
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False, help="rotators.yaml")],
    github_pat_file: Annotated[Path, typer.Option("--github-pat-file")] = Path("/var/run/secrets/github-pat/token"),
    ca_bundle: Annotated[Path, typer.Option(help="CA bundle assembled for git")] = Path("/tmp/ca-bundle.crt"),
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.model_validate(yaml.safe_load(config_path.read_text()))
    github_pat = github_pat_file.read_text().strip()

    # rules_distroless doesn't run update-ca-certificates, so /etc/ssl/certs is
    # empty. Assemble a bundle from the raw mozilla certs for git (HTTPS push).
    ca_bundle.write_text(
        "".join(p.read_text() for p in sorted(Path("/usr/share/ca-certificates/mozilla").glob("*.crt")))
    )
    os.environ["GIT_SSL_CAINFO"] = str(ca_bundle)

    repo_dir = Path("/tmp/repo")
    repo_dir.mkdir()
    os.chdir(repo_dir)
    sparse_clone(config, github_pat)

    rotated = [t.name for t in config.tokens if rotate_one(t, config)]

    if not rotated:
        logger.info("no rotations needed this cycle")
        return
    commit_and_push(config, rotated)


if __name__ == "__main__":
    typer.run(main)
