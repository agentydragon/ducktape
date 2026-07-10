"""Attic ops folded into one CLI: token rotation and cache bootstrap.

Two subcommands share the `kubectl exec deploy/attic -- atticadm make-token …`
pattern so the HS256 signing secret never leaves the attic pod:

  * `rotate`           — mints per-token JWTs into SOPS-encrypted files and
                         pushes the result to devel.
  * `bootstrap-caches` — idempotently ensures each named cache exists (creating
                         via attic's REST API when absent) and prints its public
                         key for `nix/attic-pubkeys.json`.

`rotate` reads the unencrypted-by-suffix `expires_unencrypted` field of each
existing `sops_file` (no SOPS decryption, no in-cluster age key) and skips when
remaining validity exceeds `rotate_below_hours`. With 1-year validity and a 24h
threshold, a real mint runs only ~once per token per year, but a failed
rotation self-heals on the next hourly run. Rotated files land in a single
combined commit; the whole run operates from the clone root so SOPS creation
rules (matched on repo-relative paths) resolve.
"""

import base64
import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import pygit2
import typer
import yaml
from pydantic import BaseModel, Field

from devinfra.prettier_cli import prettier_format_yaml_in_place

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False)


class Token(BaseModel):
    name: str = Field(description="Human-readable name for logs and the commit message")
    sops_file: Path = Field(description="Repo-relative path to the encrypted output file")
    sub: str = Field(description="JWT subject claim passed to atticadm --sub")
    validity: str = Field(description="Token lifetime passed to atticadm --validity (e.g. '1 year')")
    pull: list[str] = Field(description="Caches the token may pull from (atticadm --pull, repeated)")
    push: list[str] = Field(
        default_factory=list,
        description="Caches the token may push to (atticadm --push, repeated); empty for read-only tokens",
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
    git_remote: str = Field(
        default="https://github.com/agentydragon/ducktape.git",
        description="Full git remote URL to clone + push (host-agnostic; any HTTPS git server works)",
    )
    git_username: str = Field(
        default="x-access-token",
        description="Username paired with the token for HTTPS auth (GitHub PATs use 'x-access-token')",
    )
    git_clone_depth: int | None = Field(
        default=1,
        description="Shallow-clone depth for git_remote; None for a full clone (the local transport can't shallow-fetch)",
    )
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


def encrypt_sops_file(path: Path) -> None:
    subprocess.run(["sops", "encrypt", "--indent", "2", "--in-place", str(path)], check=True)


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
    encrypt_sops_file(token.sops_file)
    prettier_format_yaml_in_place(token.sops_file)
    logger.info("%s: wrote token expiring %s", token.name, expires_iso)
    return True


def _git_callbacks(username: str, token: str) -> pygit2.RemoteCallbacks:
    """pygit2 HTTPS credentials (username + token via UserPass)."""
    return pygit2.RemoteCallbacks(credentials=pygit2.UserPass(username, token))


def clone_repo(config: Config, token: str, repo_dir: Path) -> pygit2.Repository:
    """Clone `devel` of config.git_remote into repo_dir via pygit2.

    A full clone (not sparse): the rotator's token already has repo-wide access,
    the SOPS files are ciphertext on disk, and this matches the repo's other pygit2
    transport users (e.g. finance/scraper). `git_clone_depth` defaults to a shallow
    fetch over HTTPS; None does a full clone (the local transport used in tests
    can't shallow-fetch). libgit2 does its own TLS, so CA trust is wired via
    `pygit2.settings.set_ssl_cert_locations` in `main()` rather than
    `GIT_SSL_CAINFO` (which libgit2 ignores).
    """
    kwargs: dict[str, Any] = {"checkout_branch": "devel", "callbacks": _git_callbacks(config.git_username, token)}
    if config.git_clone_depth is not None:
        kwargs["depth"] = config.git_clone_depth
    return pygit2.clone_repository(config.git_remote, str(repo_dir), **kwargs)


def commit_and_push(repo: pygit2.Repository, config: Config, rotated: list[str], token: str) -> None:
    """Stage the rotated SOPS files, commit, and push to devel — all via pygit2."""
    author = pygit2.Signature(config.git_author_name, config.git_author_email)
    index = repo.index
    # Stage exactly the SOPS files for tokens that rotated this cycle (no full-tree add).
    for token_entry in config.tokens:
        if token_entry.name in rotated:
            index.add(str(token_entry.sops_file))
    index.write()
    tree_id = index.write_tree()
    if tree_id == repo.head.peel(pygit2.Commit).tree_id:
        logger.info("tokens minted but SOPS files unchanged on disk (unexpected); nothing to commit")
        return
    message = f"chore: rotate attic JWTs ({datetime.now(UTC):%Y-%m-%d}): {', '.join(rotated)}"
    repo.create_commit("HEAD", author, author, message, tree_id, [repo.head.target])
    repo.remotes["origin"].push(["refs/heads/devel"], callbacks=_git_callbacks(config.git_username, token))
    logger.info("pushed: %s", ", ".join(rotated))


def _configure_ca_trust(ca_bundle: Path) -> None:
    """Point libgit2 at the assembled CA bundle.

    libgit2 ignores GIT_SSL_CAINFO/SSL_CERT_FILE, so set_ssl_cert_locations is the
    supported knob. The dir arg must be None (NULL), not "" -- an empty string makes
    OpenSSL reject "invalid directory" while loading the bundle.
    """
    pygit2.settings.set_ssl_cert_locations(str(ca_bundle), None)


@app.command("rotate")
def rotate_cmd(
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False, help="rotators.yaml")],
    token: Annotated[str, typer.Option("--token", envvar="GIT_TOKEN", help="Git HTTPS token (or set GIT_TOKEN)")],
    ca_bundle: Annotated[Path, typer.Option(help="CA bundle assembled for git")] = Path("/tmp/ca-bundle.crt"),
) -> None:
    """Rotate every token in the rotators.yaml config; commit + push what rotated."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.model_validate(yaml.safe_load(config_path.read_text()))

    # rules_distroless doesn't run update-ca-certificates, so /etc/ssl/certs is
    # empty. Assemble a bundle from the raw mozilla certs and point libgit2 at it
    # (libgit2 ignores GIT_SSL_CAINFO/SSL_CERT_FILE; set_ssl_cert_locations is the
    # supported knob).
    ca_bundle.write_text(
        "".join(p.read_text() for p in sorted(Path("/usr/share/ca-certificates/mozilla").glob("*.crt")))
    )
    _configure_ca_trust(ca_bundle)

    repo_dir = Path("/tmp/repo")
    repo_dir.mkdir()
    os.chdir(repo_dir)
    repo = clone_repo(config, token, repo_dir)

    rotated = [t.name for t in config.tokens if rotate_one(t, config)]

    if not rotated:
        logger.info("no rotations needed this cycle")
        return
    commit_and_push(repo, config, rotated, token)


def _create_body(is_public: bool) -> dict[str, Any]:
    """Body for `POST /_api/v1/cache-config/<name>` — matches `attic cache create <name>`
    with no flags (cf. attic upstream client/src/command/cache.rs::create_cache) except
    `is_public`, which callers set explicitly.
    """
    return {
        "keypair": "Generate",
        "is_public": is_public,
        "store_dir": "/nix/store",
        "priority": 41,
        "upstream_cache_key_names": [],
    }


def mint_admin_jwt(namespace: str, deployment: str, server_config: str) -> str:
    """Mint a short-lived admin JWT via `kubectl exec deploy/attic -- atticadm make-token`.

    The HS256 signing secret never leaves the attic pod; this process only needs
    `pods/exec` on the attic deployment.
    """
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            deployment,
            "--",
            "atticadm",
            "-f",
            server_config,
            "make-token",
            "--sub",
            "bootstrap-admin",
            "--validity",
            "5 minutes",
            "--pull",
            "*",
            "--push",
            "*",
            "--create-cache",
            "*",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    jwt = result.stdout.strip()
    if not jwt:
        raise RuntimeError(f"atticadm make-token returned empty output\n{result.stderr}")
    return jwt


def ensure_cache(client: httpx.Client, cache: str, is_public: bool = False) -> str:
    """Idempotently ensure `cache` exists on the attic server behind `client`; return its public_key.

    `client` must already carry the admin Bearer token and a base URL pointing
    at the attic server. Non-2xx from the initial GET is only tolerated on 404
    (cache absent → create), everything else raises. `is_public` only affects
    creation — an already-existing cache's visibility is left as-is.
    """
    config_path = f"/_api/v1/cache-config/{cache}"
    response = client.get(config_path)
    if response.status_code == 404:
        logger.info("cache %s: missing — creating (is_public=%s)", cache, is_public)
        create = client.post(config_path, json=_create_body(is_public))
        if not create.is_success:
            raise RuntimeError(f"cache {cache}: create failed (HTTP {create.status_code}): {create.text}")
        response = client.get(config_path)
    if not response.is_success:
        raise RuntimeError(f"cache {cache}: cache-config GET failed (HTTP {response.status_code}): {response.text}")
    public_key: str = response.json()["public_key"]
    logger.info("cache %s: %s", cache, public_key)
    return public_key


@app.command("bootstrap-caches")
def bootstrap_caches_cmd(
    caches: Annotated[
        list[str] | None, typer.Option("--cache", help="Private cache name(s) to ensure; repeatable")
    ] = None,
    public_caches: Annotated[
        list[str] | None,
        typer.Option(
            "--public-cache", help="Cache name(s) to ensure with anonymous-readable is_public=true; repeatable"
        ),
    ] = None,
    server: Annotated[
        str, typer.Option(help="Attic server URL reachable in-cluster")
    ] = "http://attic.nix-cache.svc.cluster.local:8080",
    attic_namespace: Annotated[str, typer.Option(help="Namespace of the attic deployment")] = "nix-cache",
    attic_deployment: Annotated[str, typer.Option(help="Attic Deployment exec'd for atticadm")] = "deploy/attic",
    server_config: Annotated[
        str, typer.Option(help="Attic server config path inside the attic pod")
    ] = "/config/server.toml",
) -> None:
    """Ensure each --cache / --public-cache exists on `server`; print its public_key on stdout.

    Idempotent: existing caches are left alone (including their visibility — flipping
    is_public on an existing cache needs a direct API call, not this command). Public keys
    are printed for manual paste into nix/attic-pubkeys.json (attic's public
    /{cache}/nix-cache-info does not include the pubkey; the authenticated
    /_api/v1/cache-config/<cache> endpoint does).
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    jwt = mint_admin_jwt(attic_namespace, attic_deployment, server_config)
    with httpx.Client(base_url=server, headers={"Authorization": f"Bearer {jwt}"}, timeout=30.0) as client:
        for cache in caches or []:
            ensure_cache(client, cache)
        for cache in public_caches or []:
            ensure_cache(client, cache, is_public=True)


if __name__ == "__main__":
    app()
