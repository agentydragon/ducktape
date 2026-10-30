"""Attic ops folded into one CLI: token rotation and cache bootstrap.

Two subcommands share the `kubectl exec deploy/attic -- atticadm make-token …`
pattern so the HS256 signing secret never leaves the attic pod:

  * `rotate`           — mints per-token JWTs, publishes configured Kubernetes
                         Secrets, and pushes SOPS outputs to devel.
  * `bootstrap-caches` — idempotently ensures each named cache exists (creating
                         via attic's REST API when absent, with a `--keypair-dir`
                         signing key when one is mounted) and prints its public
                         key.

Deriving `nix/attic-pubkeys.json` from the signing keys is a separate, local-only
concern (`sync_pubkeys.py` in this package) — it needs SOPS decrypt access to
cluster secrets, which this file's container image intentionally does not have.

`rotate` reads expiry and scope from a configured Kubernetes Secret when present;
other tokens use the unencrypted-by-suffix stamps in their `sops_file`. It needs
no SOPS decryption or in-cluster age key. Rotated SOPS files land in one combined
commit from the clone root so creation rules matched on repo-relative paths resolve.
"""

import base64
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import pygit2
import typer
import yaml
from pydantic import BaseModel, Field, StrictStr, ValidationError

from cluster.rotators.attic_jwt_rotation.config import (
    Config,
    KubernetesDestination,
    MirroredDestination,
    SopsDestination,
    Token,
)
from devinfra.prettier_cli import prettier_format_yaml_in_place

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)

_SECRET_EXPIRY_ANNOTATION = "ducktape.allegedly.works/attic-token-expires"
_SECRET_PULL_ANNOTATION = "ducktape.allegedly.works/attic-token-pull"
_SECRET_PUSH_ANNOTATION = "ducktape.allegedly.works/attic-token-push"
_SOPS_SECRET_UID_STAMP = "kubernetes_secret_uid_unencrypted"
_SOPS_SECRET_RESOURCE_VERSION_STAMP = "kubernetes_secret_resource_version_unencrypted"


@dataclass(frozen=True)
class KubernetesSecret:
    uid: str
    resource_version: str
    jwt: str | None = field(repr=False)
    annotations: dict[str, str]


class _KubernetesSecretMetadata(BaseModel):
    name: StrictStr
    namespace: StrictStr
    uid: StrictStr = Field(min_length=1)
    resource_version: StrictStr = Field(alias="resourceVersion", min_length=1)
    annotations: dict[StrictStr, StrictStr] = Field(default_factory=dict)


class _KubernetesSecretResponse(BaseModel):
    metadata: _KubernetesSecretMetadata
    data: dict[StrictStr, StrictStr] = Field(default_factory=dict)


# JWT claims are an untyped external payload; we read a handful of well-known keys.
def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying its signature; malformed input is unusable."""
    segments = token.split(".")
    if len(segments) != 3 or not segments[1]:
        return None
    padded = segments[1] + "=" * (-len(segments[1]) % 4)
    try:
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except ValueError, UnicodeDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT's base64url payload (no signature verification)."""
    payload = _decode_jwt_payload(token)
    if payload is None:
        raise RuntimeError("atticadm returned a JWT with an invalid payload")
    return payload


def _jwt_expiry(payload: dict[str, Any], token_name: str) -> datetime:
    exp = payload.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise RuntimeError(f"{token_name}: JWT payload is missing a valid exp claim")
    try:
        return datetime.fromtimestamp(exp, UTC)
    except OverflowError, OSError, ValueError:
        raise RuntimeError(f"{token_name}: JWT exp claim is outside the supported range") from None


def _iso_expiry(expires: datetime) -> str:
    return expires.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_kubectl(description: str, args: list[str], *, input_text: str | None = None) -> str:
    """Run kubectl without exposing Secret payloads or error streams in exceptions."""
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, input=input_text, check=False)
    if result.returncode:
        raise RuntimeError(f"kubectl {description} failed (exit status {result.returncode})")
    return result.stdout


def _read_kubernetes_secret(destination: KubernetesDestination | MirroredDestination) -> KubernetesSecret | None:
    """Read the configured Secret; missing means it was not provisioned by the owner."""
    secret_destination = destination.secret
    output = _run_kubectl(
        "get Secret",
        [
            "-n",
            secret_destination.namespace,
            "get",
            "secret",
            secret_destination.name,
            "--ignore-not-found",
            "-o",
            "json",
        ],
    )
    if not output.strip():
        return None
    try:
        secret = _KubernetesSecretResponse.model_validate(json.loads(output))
    except json.JSONDecodeError, ValidationError:
        raise RuntimeError("kubectl get Secret returned an invalid object or missing metadata") from None
    metadata = secret.metadata
    if metadata.name != secret_destination.name or metadata.namespace != secret_destination.namespace:
        raise RuntimeError("kubectl get Secret returned an object outside the configured destination")
    encoded_jwt = secret.data.get(secret_destination.key)
    jwt: str | None = None
    if encoded_jwt:
        try:
            decoded_jwt = base64.b64decode(encoded_jwt, validate=True).decode("utf-8")
        except ValueError, UnicodeDecodeError:
            decoded_jwt = ""
        jwt = decoded_jwt or None
    return KubernetesSecret(metadata.uid, metadata.resource_version, jwt, dict(metadata.annotations))


def _valid_scope_annotation(secret: KubernetesSecret, annotation: str, configured: list[str]) -> bool:
    value = secret.annotations.get(annotation)
    if value is None:
        return False
    try:
        scope = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(scope, list) and all(isinstance(cache, str) for cache in scope) and set(scope) == set(configured)


def _secret_expiry(secret: KubernetesSecret) -> datetime | None:
    if secret.jwt is None:
        return None
    payload = _decode_jwt_payload(secret.jwt)
    if payload is None:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool):
        return None
    try:
        expires = datetime.fromtimestamp(exp, UTC)
    except OverflowError, OSError, ValueError:
        return None
    if secret.annotations.get(_SECRET_EXPIRY_ANNOTATION) != _iso_expiry(expires):
        return None
    return expires


def _secret_rotation_reason(token: Token, secret: KubernetesSecret, rotate_below_hours: int) -> str | None:
    if secret.jwt is None:
        return "Secret has no usable token"
    payload = _decode_jwt_payload(secret.jwt)
    if payload is None:
        return "Secret token payload is invalid"
    if payload.get("sub") != token.sub:
        return "Secret token subject differs from configuration"
    expires = _secret_expiry(secret)
    if expires is None:
        return "Secret expiry metadata is missing or inconsistent"
    if not _valid_scope_annotation(secret, _SECRET_PULL_ANNOTATION, token.pull):
        return "Secret pull scope differs from configuration"
    if not _valid_scope_annotation(secret, _SECRET_PUSH_ANNOTATION, token.push):
        return "Secret push scope differs from configuration"
    remaining = (expires - datetime.now(UTC)).total_seconds() / 3600
    if remaining <= rotate_below_hours:
        return f"{remaining:.0f}h remaining"
    return None


def _secret_patch(
    token: Token, destination: KubernetesDestination | MirroredDestination, jwt: str, expires: datetime
) -> KubernetesSecret:
    secret_destination = destination.secret
    patch = {
        "data": {secret_destination.key: base64.b64encode(jwt.encode("utf-8")).decode("ascii")},
        "metadata": {
            "annotations": {
                _SECRET_EXPIRY_ANNOTATION: _iso_expiry(expires),
                _SECRET_PULL_ANNOTATION: json.dumps(sorted(token.pull), separators=(",", ":")),
                _SECRET_PUSH_ANNOTATION: json.dumps(sorted(token.push), separators=(",", ":")),
            }
        },
    }
    _run_kubectl(
        "patch Secret",
        [
            "-n",
            secret_destination.namespace,
            "patch",
            "secret",
            secret_destination.name,
            "--type=merge",
            "--patch-file=/dev/stdin",
        ],
        input_text=json.dumps(patch),
    )
    updated = _read_kubernetes_secret(destination)
    if updated is None or updated.jwt != jwt:
        raise RuntimeError(f"{token.name}: Kubernetes Secret patch did not persist the minted token")
    return updated


def _notify_workspace(destination: KubernetesDestination | MirroredDestination, secret: KubernetesSecret) -> None:
    notification = destination.notification
    patch = {"metadata": {"annotations": {notification.annotation_key: f"{secret.uid}/{secret.resource_version}"}}}
    _run_kubectl(
        "patch Workspace",
        ["patch", "workspaces.opentofu.upbound.io", notification.workspace, "--type=merge", "--patch-file=/dev/stdin"],
        input_text=json.dumps(patch),
    )


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


def scope_drift(token: Token, stamps: dict[str, Any]) -> str | None:
    """Why the stamped scope diverges from the configured one, or None if it matches.

    Stamps are YAML lists compared as sets (order-insensitive). A stamp missing or
    malformed (pre-scope-tracking file) counts as drift, so the first run after this
    feature ships re-mints every token once and stamps it.
    """
    for stamp_key, configured in (("pull_unencrypted", token.pull), ("push_unencrypted", token.push)):
        stamped = stamps.get(stamp_key)
        if not isinstance(stamped, list) or not all(isinstance(cache, str) for cache in stamped):
            return f"{stamp_key}: stamped scope is missing or malformed"
        if set(stamped) != set(configured):
            return f"{stamp_key}: stamped {stamped!r} != configured {configured!r}"
    return None


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
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{token.name}: atticadm make-token failed (exit status {result.returncode})")
    jwt = result.stdout.strip()
    if not jwt:
        raise RuntimeError(f"{token.name}: atticadm make-token returned empty output")
    return jwt


def encrypt_sops_file(path: Path) -> None:
    subprocess.run(["sops", "encrypt", "--indent", "2", "--in-place", str(path)], check=True)


def _write_sops_token(
    token: Token,
    destination: SopsDestination | MirroredDestination,
    config: Config,
    jwt: str,
    expires: datetime,
    secret: KubernetesSecret | None = None,
) -> None:
    # `*_unencrypted` keys match SOPS's default unencrypted_suffix, so they stay
    # plaintext after encryption and can be checked without the in-cluster age key.
    stamps: dict[str, Any] = {
        "expires_unencrypted": _iso_expiry(expires),
        "pull_unencrypted": sorted(token.pull),
        "push_unencrypted": sorted(token.push),
        config.token_field: jwt,
    }
    if secret is not None:
        stamps[_SOPS_SECRET_UID_STAMP] = secret.uid
        stamps[_SOPS_SECRET_RESOURCE_VERSION_STAMP] = secret.resource_version
    destination.sops_file.parent.mkdir(parents=True, exist_ok=True)
    destination.sops_file.write_text(yaml.safe_dump(stamps, sort_keys=False, width=2**31))
    try:
        encrypt_sops_file(destination.sops_file)
        prettier_format_yaml_in_place(destination.sops_file)
    except subprocess.CalledProcessError, OSError:
        destination.sops_file.unlink(missing_ok=True)
        raise RuntimeError(f"{token.name}: failed to encrypt or format SOPS token output") from None
    logger.info("%s: wrote token expiring %s", token.name, _iso_expiry(expires))


def _sops_matches_secret(token: Token, stamps: dict[str, Any], secret: KubernetesSecret, expires: datetime) -> bool:
    return (
        stamps.get("expires_unencrypted") == _iso_expiry(expires)
        and scope_drift(token, stamps) is None
        and stamps.get(_SOPS_SECRET_UID_STAMP) == secret.uid
        and stamps.get(_SOPS_SECRET_RESOURCE_VERSION_STAMP) == secret.resource_version
    )


def _rotate_kubernetes_token(
    token: Token, destination: KubernetesDestination | MirroredDestination, config: Config
) -> bool:
    secret = _read_kubernetes_secret(destination)
    if secret is None:
        raise RuntimeError(
            f"{token.name}: Kubernetes Secret {destination.secret.namespace}/{destination.secret.name} "
            "is missing; provision it before rotating"
        )

    expires: datetime | None
    reason = _secret_rotation_reason(token, secret, config.rotate_below_hours)
    if reason is not None:
        logger.info("%s: rotating (%s)", token.name, reason)
        jwt = mint_attic_token(token, config)
        payload = jwt_payload(jwt)
        if payload.get("sub") != token.sub:
            raise RuntimeError(f"{token.name}: minted JWT subject differs from configuration")
        expires = _jwt_expiry(payload, token.name)
        secret = _secret_patch(token, destination, jwt, expires)
        if _secret_rotation_reason(token, secret, config.rotate_below_hours) is not None:
            raise RuntimeError(f"{token.name}: Kubernetes Secret does not match the minted token and configured scope")
    else:
        expires = _secret_expiry(secret)
        if expires is None or secret.jwt is None:
            raise RuntimeError(f"{token.name}: Kubernetes Secret changed while it was being validated")
        jwt = secret.jwt

    # This is intentionally retried on every run. The Workspace's annotation
    # watch is the trigger for the existing Crossplane GitOps sync.
    _notify_workspace(destination, secret)

    if not isinstance(destination, MirroredDestination):
        return False
    if reason is not None:
        sops_updated = True
    else:
        stamps = unencrypted_stamps(destination.sops_file)
        sops_updated = not _sops_matches_secret(token, stamps, secret, expires)
    if sops_updated:
        if reason is None:
            logger.info("%s: syncing SOPS output from the current Kubernetes Secret", token.name)
        _write_sops_token(token, destination, config, jwt, expires, secret)
    return sops_updated


def rotate_one(token: Token, config: Config) -> bool:
    """Rotate one token; return whether the SOPS file needs the combined commit."""
    destination = token.destination
    if isinstance(destination, (KubernetesDestination, MirroredDestination)):
        return _rotate_kubernetes_token(token, destination, config)

    stamps = unencrypted_stamps(destination.sops_file)
    remaining = remaining_hours(destination.sops_file)
    drift = scope_drift(token, stamps)
    if remaining is not None and remaining > config.rotate_below_hours and drift is None:
        logger.info("%s: %.0fh remaining > %dh threshold; skipping", token.name, remaining, config.rotate_below_hours)
        return False
    reason = drift or ("no existing token" if remaining is None else f"{remaining:.0f}h remaining")
    logger.info("%s: rotating (%s)", token.name, reason)

    jwt = mint_attic_token(token, config)
    payload = jwt_payload(jwt)
    expires = _jwt_expiry(payload, token.name)
    _write_sops_token(token, destination, config, jwt, expires)
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
        destination = token_entry.destination
        if token_entry.name in rotated and isinstance(destination, (SopsDestination, MirroredDestination)):
            index.add(str(destination.sops_file))
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
    """Point libgit2 at the CA bundle.

    libgit2 ignores GIT_SSL_CAINFO/SSL_CERT_FILE, so set_ssl_cert_locations is the
    supported knob. The dir arg must be None (NULL), not "" -- an empty string makes
    OpenSSL reject "invalid directory" while loading the bundle.
    """
    pygit2.settings.set_ssl_cert_locations(str(ca_bundle), None)


@app.command("rotate")
def rotate_cmd(
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False, help="rotators.yaml")],
    token: Annotated[str, typer.Option("--token", envvar="GIT_TOKEN", help="Git HTTPS token (or set GIT_TOKEN)")],
) -> None:
    """Rotate every token in the rotators.yaml config; commit + push what rotated."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.model_validate(yaml.safe_load(config_path.read_text()))

    # The image bakes /etc/ssl/certs/ca-certificates.crt via
    # //third_party/debian_slim:cacerts; point libgit2 at it (it ignores
    # GIT_SSL_CAINFO/SSL_CERT_FILE).
    _configure_ca_trust(Path("/etc/ssl/certs/ca-certificates.crt"))

    repo_dir = Path("/tmp/repo")
    repo_dir.mkdir()
    os.chdir(repo_dir)
    repo = clone_repo(config, token, repo_dir)

    rotated = [t.name for t in config.tokens if rotate_one(t, config)]

    if not rotated:
        logger.info("no SOPS changes to commit")
        return
    commit_and_push(repo, config, rotated, token)


def _create_body(is_public: bool, keypair: str | None = None) -> dict[str, Any]:
    """Body for `POST /_api/v1/cache-config/<name>` — matches `attic cache create <name>`
    with no flags (cf. attic upstream client/src/command/cache.rs::create_cache) except
    `is_public`, which callers set explicitly.

    `keypair`, when given, is a `nix key generate-secret` string (`<name>:<base64>`)
    supplied verbatim to attic's `KeypairConfig::Keypair` variant instead of
    `Generate` — see cluster/docs/nix_cache.md § Bootstrap for why.
    """
    return {
        "keypair": {"Keypair": keypair} if keypair is not None else "Generate",
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


def ensure_cache(client: httpx.Client, cache: str, is_public: bool = False, keypair: str | None = None) -> str:
    """Idempotently ensure `cache` exists on the attic server behind `client`; return its public_key.

    `client` must already carry the admin Bearer token and a base URL pointing
    at the attic server. Non-2xx from the initial GET is only tolerated on 404
    (cache absent → create), everything else raises. `is_public` and `keypair`
    only affect creation — an already-existing cache's config is left as-is.
    """
    config_path = f"/_api/v1/cache-config/{cache}"
    response = client.get(config_path)
    if response.status_code == 404:
        logger.info(
            "cache %s: missing — creating (is_public=%s, supplied_key=%s)", cache, is_public, keypair is not None
        )
        create = client.post(config_path, json=_create_body(is_public, keypair))
        if not create.is_success:
            raise RuntimeError(f"cache {cache}: create failed (HTTP {create.status_code}): {create.text}")
        response = client.get(config_path)
    if not response.is_success:
        raise RuntimeError(f"cache {cache}: cache-config GET failed (HTTP {response.status_code}): {response.text}")
    public_key: str = response.json()["public_key"]
    logger.info("cache %s: %s", cache, public_key)
    return public_key


def _load_keypair(keypair_dir: Path | None, cache: str) -> str | None:
    """The supplied `nix key generate-secret` string for `cache`, or None to let attic generate one.

    `keypair_dir` is a directory of per-cache key files (Secret volume mount, filename
    = cache name) — absent entirely, or missing a given cache's file, falls back to
    server-generated keys.
    """
    if keypair_dir is None:
        return None
    keypair_file = keypair_dir / cache
    if not keypair_file.exists():
        return None
    return keypair_file.read_text().strip()


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
    keypair_dir: Annotated[
        Path | None,
        typer.Option(
            help="Directory of mounted per-cache signing keys (filename = cache name; "
            "cluster/k8s/nix-cache/cache-keys.sops.yaml), supplied on creation instead of "
            "a server-generated key. A cache missing from the directory still gets one generated."
        ),
    ] = None,
) -> None:
    """Ensure each --cache / --public-cache exists on `server`; print its public_key on stdout.

    Idempotent: existing caches are left alone (including their visibility and signing
    key — flipping is_public, or swapping in a supplied key, on an existing cache needs a
    direct API call, not this command). Public keys are printed for
    nix/attic-pubkeys.json — regenerate that file with
    cluster/rotators/attic_jwt_rotation/sync_pubkeys.py rather than pasting these by hand.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    jwt = mint_admin_jwt(attic_namespace, attic_deployment, server_config)
    with httpx.Client(base_url=server, headers={"Authorization": f"Bearer {jwt}"}, timeout=30.0) as client:
        for cache in caches or []:
            ensure_cache(client, cache, keypair=_load_keypair(keypair_dir, cache))
        for cache in public_caches or []:
            ensure_cache(client, cache, is_public=True, keypair=_load_keypair(keypair_dir, cache))


if __name__ == "__main__":
    app()
