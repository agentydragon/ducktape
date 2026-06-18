"""Rotate Authentik client_credentials JWTs into SOPS-encrypted files.

One run processes every rotation listed in the YAML config. For each rotation it:

  1. Reads the unencrypted-by-suffix `expires_unencrypted` field of the existing
     `sops_file` (no SOPS decryption, no in-cluster age key) and skips when
     remaining validity exceeds `rotate_below_hours`. A real mint therefore
     happens only every ~44 days (45d validity - 1d threshold), but a failed
     rotation self-heals on the next hourly run.
  2. Mints a fresh JWT via `client_credentials`, verifies its issuer and an
     optional group claim, and — for proxy-fronted consumers — exchanges it
     into a proxy-provider-scoped token (the JWT-bearer two-hop pattern the
     Authentik outposts in this repo require).
  3. Writes `expires_unencrypted` (from the final token's own `exp` claim, so
     the freshness check is authoritative) plus the token, then `sops encrypt`s
     in place.

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
from typing import Annotated, Any, Literal

import httpx
import typer
import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

AUTH_BASE = "https://auth.allegedly.works"
TOKEN_URL = f"{AUTH_BASE}/application/o/token/"

CredentialMode = Literal["client_secret", "user_password"]


class Rotation(BaseModel):
    name: str = Field(description="Human-readable name for logs and the commit message")
    provider_slug: str = Field(description="Authentik provider slug; pins the expected source-JWT issuer")
    scopes: str = Field(description="OAuth scopes for the client_credentials mint")
    credential_mode: CredentialMode = Field(
        default="client_secret",
        description="How to authenticate to the token endpoint: provider client_secret or service-account username/password",
    )
    credentials_dir: Path = Field(
        description="Mounted secret dir holding client credentials (+ proxy_client_id when exchanging)"
    )
    sops_file: Path = Field(description="Repo-relative path to the encrypted output file")
    token_field: str = Field(description="YAML field name under which the token is written")
    expected_group: str | None = Field(
        default=None, description="Group claim that must be present on the source JWT; aborts the rotation if missing"
    )
    exchange_scopes: str | None = Field(
        default=None,
        description="When set, exchange the source JWT into a proxy-provider token "
        "(client_id from credentials_dir/proxy_client_id) with these scopes",
    )

    @property
    def expected_issuer(self) -> str:
        return f"{AUTH_BASE}/application/o/{self.provider_slug}/"


class Config(BaseModel):
    rotations: list[Rotation]
    rotate_below_hours: int = Field(
        default=24, description="Mint a fresh token once remaining validity drops below this"
    )
    github_repo: str = "agentydragon/ducktape"
    sops_config: str = Field(default=".sops.yaml", description="Repo path sops reads to pick the recipient set")
    git_author_name: str = "authentik-jwt-rotation"
    git_author_email: str = "noreply@allegedly.works"


# JWT claims are an untyped external payload; we read a handful of well-known keys.
def jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT's base64url payload (no signature verification)."""
    segment = token.split(".")[1]
    padded = segment + "=" * (-len(segment) % 4)
    payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return payload


def remaining_hours(sops_file: Path) -> float | None:
    """Hours until the existing token expires, or None if absent/unstamped."""
    if not sops_file.exists():
        return None
    for line in sops_file.read_text().splitlines():
        if line.startswith("expires_unencrypted:"):
            value = line.split(":", 1)[1].strip().strip('"')
            expires = datetime.fromisoformat(value)
            return (expires - datetime.now(UTC)).total_seconds() / 3600
    return None


def mint_jwt(client: httpx.Client, rotation: Rotation) -> str:
    client_id = (rotation.credentials_dir / "client_id").read_text().strip()
    data = {"grant_type": "client_credentials", "scope": rotation.scopes}

    if rotation.credential_mode == "client_secret":
        client_secret = (rotation.credentials_dir / "client_secret").read_text().strip()
        resp = client.post(TOKEN_URL, auth=(client_id, client_secret), data=data)
    else:
        username = (rotation.credentials_dir / "username").read_text().strip()
        password = (rotation.credentials_dir / "password").read_text().strip()
        resp = client.post(TOKEN_URL, data={**data, "client_id": client_id, "username": username, "password": password})

    resp.raise_for_status()
    token: str | None = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"{rotation.name}: client_credentials returned no access_token")
    return token


def exchange_jwt(client: httpx.Client, rotation: Rotation, source_jwt: str, exchange_client_id: str) -> str:
    resp = client.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": exchange_client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": source_jwt,
            "scope": rotation.exchange_scopes,
        },
    )
    resp.raise_for_status()
    token: str | None = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"{rotation.name}: proxy-provider exchange returned no access_token")
    if token == source_jwt:
        raise RuntimeError(f"{rotation.name}: proxy-provider exchange returned the source JWT unchanged")
    return token


def rotate_one(client: httpx.Client, rotation: Rotation, config: Config) -> bool:
    """Mint + write a fresh token for one rotation. Returns True if it wrote."""
    remaining = remaining_hours(rotation.sops_file)
    if remaining is not None and remaining > config.rotate_below_hours:
        logger.info(
            "%s: %.0fh remaining > %dh threshold; skipping", rotation.name, remaining, config.rotate_below_hours
        )
        return False
    logger.info("%s: rotating (remaining=%s)", rotation.name, "none" if remaining is None else f"{remaining:.0f}h")

    source_jwt = mint_jwt(client, rotation)
    source_payload = jwt_payload(source_jwt)
    if source_payload.get("iss") != rotation.expected_issuer:
        raise RuntimeError(
            f"{rotation.name}: source JWT issuer {source_payload.get('iss')!r} != {rotation.expected_issuer!r}"
        )
    groups = source_payload.get("groups") or []
    if rotation.expected_group and rotation.expected_group not in groups:
        raise RuntimeError(
            f"{rotation.name}: source JWT missing expected group {rotation.expected_group!r} (got {groups!r})"
        )

    if rotation.exchange_scopes:
        exchange_client_id = (rotation.credentials_dir / "proxy_client_id").read_text().strip()
        final_jwt = exchange_jwt(client, rotation, source_jwt, exchange_client_id)
    else:
        final_jwt = source_jwt

    # `exp` from the token we actually write — the proxy provider's lifetime can
    # differ from the source provider's, so freshness must key off the final token.
    expires_iso = datetime.fromtimestamp(int(jwt_payload(final_jwt)["exp"]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # `expires_unencrypted` matches SOPS's default unencrypted_suffix, so it
    # stays plaintext after `sops encrypt --in-place`.
    rotation.sops_file.parent.mkdir(parents=True, exist_ok=True)
    rotation.sops_file.write_text(f'expires_unencrypted: "{expires_iso}"\n{rotation.token_field}: {final_jwt}\n')
    subprocess.run(["sops", "encrypt", "--in-place", str(rotation.sops_file)], check=True)
    logger.info("%s: wrote token expiring %s", rotation.name, expires_iso)
    return True


def sparse_clone(config: Config, github_pat: str) -> None:
    """Init + sparse-fetch only .sops.yaml and the rotations' sops_files into cwd."""
    remote = f"https://x-access-token:{github_pat}@github.com/{config.github_repo}.git"
    sparse_paths = [config.sops_config, *(str(r.sops_file) for r in config.rotations)]
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], check=True)
    subprocess.run(["git", "config", "core.sparseCheckout", "true"], check=True)
    Path(".git/info/sparse-checkout").write_text("\n".join(sparse_paths) + "\n")
    subprocess.run(["git", "fetch", "-q", "--depth=1", "--no-tags", "origin", "devel"], check=True)
    subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], check=True)


def commit_and_push(config: Config, rotated: list[str]) -> None:
    subprocess.run(["git", "config", "user.name", config.git_author_name], check=True)
    subprocess.run(["git", "config", "user.email", config.git_author_email], check=True)
    for rotation in config.rotations:
        if rotation.name in rotated:
            subprocess.run(["git", "add", "--", str(rotation.sops_file)], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        logger.info("tokens minted but SOPS files unchanged on disk (unexpected); nothing to commit")
        return
    message = f"chore: rotate authentik JWTs ({datetime.now(UTC):%Y-%m-%d}): {', '.join(rotated)}"
    subprocess.run(["git", "commit", "-q", "-m", message], check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:devel"], check=True)
    logger.info("pushed: %s", ", ".join(rotated))


def main(
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False, help="rotations.yaml")],
    github_pat_file: Annotated[Path, typer.Option("--github-pat-file")] = Path("/var/run/secrets/github-pat/token"),
    ca_bundle: Annotated[Path, typer.Option(help="CA bundle assembled for git + httpx")] = Path("/tmp/ca-bundle.crt"),
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.model_validate(yaml.safe_load(config_path.read_text()))
    github_pat = github_pat_file.read_text().strip()

    # rules_distroless doesn't run update-ca-certificates, so /etc/ssl/certs is
    # empty. Assemble a bundle from the raw mozilla certs for git (HTTPS push)
    # and httpx to share.
    ca_bundle.write_text(
        "".join(p.read_text() for p in sorted(Path("/usr/share/ca-certificates/mozilla").glob("*.crt")))
    )
    os.environ["GIT_SSL_CAINFO"] = str(ca_bundle)

    repo_dir = Path("/tmp/repo")
    repo_dir.mkdir()
    os.chdir(repo_dir)
    sparse_clone(config, github_pat)

    with httpx.Client(verify=str(ca_bundle), timeout=30) as client:
        rotated = [r.name for r in config.rotations if rotate_one(client, r, config)]

    if not rotated:
        logger.info("no rotations needed this cycle")
        return
    commit_and_push(config, rotated)


if __name__ == "__main__":
    typer.run(main)
