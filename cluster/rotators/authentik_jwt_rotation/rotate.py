"""Rotate Authentik client_credentials JWTs into SOPS-encrypted files.

One run processes every rotation listed in the YAML config. For each rotation it:

  1. Reads the unencrypted-by-suffix `expires_unencrypted` field of the existing
     `sops_file` (no SOPS decryption, no in-cluster age key) and skips when
     remaining validity exceeds `rotate_below_hours`. A real mint therefore
     happens only every ~44 days (45d validity - 1d threshold), but a failed
     rotation self-heals on the next hourly run.
  2. Mints a fresh JWT via `client_credentials`, verifies its issuer, an
     optional group claim, an optional set of expected audiences, and optional
     expected claims (e.g. email) on the final token, and — for proxy-fronted
     consumers — exchanges it into a proxy-provider-scoped token (the
     JWT-bearer two-hop pattern the Authentik outposts in this repo require).
  3. Writes `expires_unencrypted` (from the final token's own `exp` claim, so
     the freshness check is authoritative), an `audiences_unencrypted` stamp
     when audiences are expected and a `claims_unencrypted` stamp when claims
     are expected (so the next run can force a re-mint if either expectation
     changes — e.g. an Authentik service account's email gets fixed), plus the
     token, then `sops encrypt`s in place.

Rotations with a `probe` additionally verify on every run that the token
*published in-cluster* (the `k8s_secret` output, read back via the Kubernetes
API) is actually accepted by its real endpoint; a 401/403 bypasses the
freshness gate exactly like an audience/claim mismatch. Stamps only prove the
token should still be valid — the probe catches provider-side revocation and
publish-pipeline bugs (a seed placeholder never overwritten, a manifest
written under the wrong stringData key).

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
from typing import Annotated, Any, Literal, Self

import httpx
import typer
import yaml
from pydantic import BaseModel, Field, model_validator

from devinfra.prettier_cli import prettier_format_yaml_in_place

logger = logging.getLogger(__name__)

AUTH_BASE = "https://auth.allegedly.works"
TOKEN_URL = f"{AUTH_BASE}/application/o/token/"

K8S_API = "https://kubernetes.default.svc"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

CredentialMode = Literal["client_secret", "user_password"]


class K8sSecretOutput(BaseModel):
    """Optional second output: write the minted token as a k8s Secret manifest.

    Lets a rotation feed an in-cluster consumer (Flux decrypts + applies the
    Secret) without that consumer ever touching SOPS. The `path` lives under
    `cluster/k8s/` so the `.sops.yaml` cluster catch-all rule encrypts stringData
    for Flux + admin. The token's `exp` epoch is written alongside so consumers
    that need a monotonic "the token changed" signal (e.g. a Terraform write-only
    `*_wo_version`) have one without decrypting the token.
    """

    path: Path = Field(description="Repo-relative path for the Secret manifest (under cluster/k8s/, *.sops.yaml)")
    name: str
    namespace: str
    token_key: str = Field(default="jwt", description="stringData key for the JWT")
    exp_key: str = Field(default="token-exp", description="stringData key for the JWT exp epoch (seconds)")


class Probe(BaseModel):
    """Live check that the *published* token still works against its real endpoint.

    The job holds no age key, so the sops file cannot be read back; the probe
    instead reads the in-cluster Secret the `k8s_secret` output feeds and sends
    its token as a Bearer to `url`. Only an explicit 401/403 is a credential
    verdict — anything else means the request made it past auth (or the endpoint
    is sick, which is not the token's fault).
    """

    url: str = Field(description="Endpoint that answers 401/403 iff the bearer is bad")
    method: Literal["GET", "POST"] = "GET"


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
    expected_audiences: list[str] | None = Field(
        default=None,
        description="Audiences (aud claim) that must all be present on the final written token. "
        "Asserted on every mint, and forces a re-mint when the stored token's "
        "audiences_unencrypted stamp does not already cover them — so adding an audience "
        "rolls out on the next run instead of waiting for expiry.",
    )
    expected_claims: dict[str, str] | None = Field(
        default=None,
        description="Other claims (e.g. email) that must equal the given value on the final "
        "written token. Asserted on every mint, and forces a re-mint when the stored token's "
        "claims_unencrypted stamp does not already match — so fixing the upstream Authentik "
        "attribute (e.g. a service account's missing email) rolls out on the next run instead "
        "of waiting for expiry.",
    )
    exchange_scopes: str | None = Field(
        default=None,
        description="When set, exchange the source JWT into a proxy-provider token "
        "(client_id from credentials_dir/proxy_client_id) with these scopes",
    )
    k8s_secret: K8sSecretOutput | None = Field(
        default=None,
        description="When set, also write the minted token as a k8s Secret manifest (in addition "
        "to sops_file) for an in-cluster consumer to read via Flux.",
    )
    probe: Probe | None = Field(
        default=None,
        description="When set (requires k8s_secret), read the published Secret back each run and "
        "verify its token against the real endpoint; a 401/403 forces a re-mint.",
    )

    @model_validator(mode="after")
    def _probe_requires_k8s_secret(self) -> Self:
        if self.probe and not self.k8s_secret:
            raise ValueError(f"{self.name}: probe requires k8s_secret (the sops file cannot be read back)")
        return self

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


def unencrypted_stamps(sops_file: Path) -> dict[str, Any]:
    """The plaintext `*_unencrypted` stamps from a sops file, parsed as YAML.

    A sops-encrypted YAML file is still valid YAML, and the `*_unencrypted`-suffixed
    keys keep their plaintext values (SOPS leaves them in clear), so the freshness
    and audience stamps load straight out without the in-cluster age key. Returns
    `{}` when the file is absent or empty.
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


def token_audiences(payload: dict[str, Any]) -> list[str]:
    """The `aud` claim normalized to a list (the JWT spec allows a string or a list)."""
    aud = payload.get("aud")
    if aud is None:
        return []
    return [aud] if isinstance(aud, str) else list(aud)


def stamped_audiences(sops_file: Path) -> list[str] | None:
    """Audiences from the plaintext `audiences_unencrypted` stamp, or None if absent.

    Lets the freshness check notice an audience-expectation change without
    decrypting the token (the rotator has no in-cluster age key).
    """
    return unencrypted_stamps(sops_file).get("audiences_unencrypted")


def stamped_claims(sops_file: Path) -> dict[str, str] | None:
    """Claims from the plaintext `claims_unencrypted` stamp, or None if absent.

    Lets the freshness check notice a claim-expectation change (e.g. an Authentik
    service account's email getting fixed) without decrypting the token.
    """
    return unencrypted_stamps(sops_file).get("claims_unencrypted")


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


def encrypt_sops_file(path: Path) -> None:
    subprocess.run(["sops", "encrypt", "--indent", "2", "--in-place", str(path)], check=True)


def published_secret_data(out: K8sSecretOutput) -> dict[str, str] | None:
    """`data` of the published in-cluster Secret (base64 values), or None if unreadable.

    None means "no verdict" — SA token not mounted, API unreachable, or the
    Secret not applied yet (Flux lag after a recent mint). The caller skips the
    probe rather than mint-churning hourly while the publish pipeline is
    mid-flight or down; the warning in the Job log is the alarm for that case.
    """
    sa_token_file = SA_DIR / "token"
    if not sa_token_file.exists():
        logger.warning("%s/%s: no ServiceAccount token mounted; skipping probe", out.namespace, out.name)
        return None
    try:
        resp = httpx.get(
            f"{K8S_API}/api/v1/namespaces/{out.namespace}/secrets/{out.name}",
            headers={"Authorization": f"Bearer {sa_token_file.read_text().strip()}"},
            verify=str(SA_DIR / "ca.crt"),
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("%s/%s: cannot read published Secret (%r); skipping probe", out.namespace, out.name, e)
        return None
    data: dict[str, str] = resp.json().get("data") or {}
    return data


def probe_rejects_token(client: httpx.Client, probe: Probe, token: str) -> bool:
    """True iff the endpoint explicitly rejects the bearer (401/403).

    Endpoint sickness (5xx, network error) is not a credential verdict: return
    False so an outage doesn't cause hourly mint churn. Any other status means
    the request made it past auth.
    """
    try:
        resp = client.request(probe.method, probe.url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        logger.warning("probe %s: unreachable (%r); no credential verdict", probe.url, e)
        return False
    if resp.status_code in (401, 403):
        return True
    if resp.status_code >= 500:
        logger.warning("probe %s: HTTP %d (endpoint sick); no credential verdict", probe.url, resp.status_code)
    return False


def published_token_rejected(client: httpx.Client, name: str, out: K8sSecretOutput, probe: Probe) -> bool:
    """True iff the published Secret is demonstrably broken: missing the configured
    stringData key, or its token rejected by the probe endpoint. An unreadable
    Secret is no verdict (False) — see published_secret_data."""
    if (data := published_secret_data(out)) is None:
        return False
    if (token_b64 := data.get(out.token_key)) is None:
        logger.info("%s: published Secret lacks stringData key %r; forcing re-mint", name, out.token_key)
        return True
    if probe_rejects_token(client, probe, base64.b64decode(token_b64).decode()):
        logger.info("%s: published token rejected by %s; forcing re-mint", name, probe.url)
        return True
    return False


def rotate_one(client: httpx.Client, rotation: Rotation, config: Config) -> bool:
    """Mint + write a fresh token for one rotation. Returns True if it wrote."""
    remaining = remaining_hours(rotation.sops_file)
    fresh = remaining is not None and remaining > config.rotate_below_hours
    # An audience expectation the stored token doesn't already satisfy forces a
    # re-mint regardless of remaining validity — otherwise adding an audience
    # wouldn't take effect until the (~44-day) expiry. Read from the plaintext
    # `audiences_unencrypted` stamp (no SOPS decryption needed).
    if fresh and rotation.expected_audiences:
        stored = stamped_audiences(rotation.sops_file)
        if stored is None or not set(rotation.expected_audiences) <= set(stored):
            logger.info(
                "%s: stored audiences %s do not cover expected %s; forcing re-mint",
                rotation.name,
                stored,
                rotation.expected_audiences,
            )
            fresh = False
    # Same idea as the audiences check above, for arbitrary claims (e.g. email):
    # a stored token minted before an upstream Authentik fix landed would otherwise
    # keep being treated as fresh until its ~44-day expiry.
    if fresh and rotation.expected_claims:
        stored_claims = stamped_claims(rotation.sops_file)
        if stored_claims is None or stored_claims != rotation.expected_claims:
            logger.info(
                "%s: stored claims %s do not match expected %s; forcing re-mint",
                rotation.name,
                stored_claims,
                rotation.expected_claims,
            )
            fresh = False
    # Stamps only prove the token *should* still be valid; the probe checks that
    # the published artifact actually works end-to-end.
    if fresh and rotation.probe and rotation.k8s_secret:
        fresh = not published_token_rejected(client, rotation.name, rotation.k8s_secret, rotation.probe)
    if fresh:
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

    final_payload = jwt_payload(final_jwt)
    final_audiences = token_audiences(final_payload)
    if rotation.expected_audiences:
        missing = set(rotation.expected_audiences) - set(final_audiences)
        if missing:
            raise RuntimeError(
                f"{rotation.name}: minted token missing expected audiences {sorted(missing)} "
                f"(got {final_audiences!r}); check the Authentik provider's audience mapping"
            )
    if rotation.expected_claims:
        mismatched = {
            claim: (expected, final_payload.get(claim))
            for claim, expected in rotation.expected_claims.items()
            if final_payload.get(claim) != expected
        }
        if mismatched:
            raise RuntimeError(
                f"{rotation.name}: minted token claims mismatch {mismatched} "
                f"(expected -> got); check the Authentik user/scope-mapping attributes"
            )

    # `exp` from the token we actually write — the proxy provider's lifetime can
    # differ from the source provider's, so freshness must key off the final token.
    expires_iso = datetime.fromtimestamp(int(final_payload["exp"]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # `*_unencrypted` keys match SOPS's default unencrypted_suffix, so they stay
    # plaintext after `sops encrypt --in-place`. The audiences stamp lets the next
    # run detect an audience-expectation change without decrypting the token.
    stamps: dict[str, Any] = {"expires_unencrypted": expires_iso}
    if rotation.expected_audiences:
        stamps["audiences_unencrypted"] = final_audiences
    if rotation.expected_claims:
        stamps["claims_unencrypted"] = {claim: final_payload.get(claim) for claim in rotation.expected_claims}
    stamps[rotation.token_field] = final_jwt
    rotation.sops_file.parent.mkdir(parents=True, exist_ok=True)
    rotation.sops_file.write_text(yaml.safe_dump(stamps, sort_keys=False, width=2**31))
    encrypt_sops_file(rotation.sops_file)
    prettier_format_yaml_in_place(rotation.sops_file)
    logger.info("%s: wrote token expiring %s", rotation.name, expires_iso)

    if rotation.k8s_secret:
        write_k8s_secret(rotation.k8s_secret, final_jwt, int(final_payload["exp"]))
        logger.info("%s: wrote k8s Secret %s", rotation.name, rotation.k8s_secret.path)
    return True


def build_secret_manifest(out: K8sSecretOutput, token: str, exp_epoch: int) -> dict[str, Any]:
    """The k8s Secret manifest carrying the token + exp (pre-encryption)."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": out.name,
            "namespace": out.namespace,
            "annotations": {"description": "Authentik JWT minted by authentik-jwt-rotation (rotated ~biweekly)."},
        },
        "type": "Opaque",
        "stringData": {out.token_key: token, out.exp_key: str(exp_epoch)},
    }


def write_k8s_secret(out: K8sSecretOutput, token: str, exp_epoch: int) -> None:
    """Write `out.path` as a SOPS-encrypted k8s Secret manifest carrying the token + exp.

    stringData is encrypted by the `.sops.yaml` cluster catch-all rule; metadata
    stays plaintext. The exp epoch is the monotonic signal a consumer uses to know
    the token rotated (e.g. a Terraform write-only `*_wo_version`).
    """
    out.path.parent.mkdir(parents=True, exist_ok=True)
    out.path.write_text(yaml.safe_dump(build_secret_manifest(out, token, exp_epoch), sort_keys=False, width=2**31))
    encrypt_sops_file(out.path)
    prettier_format_yaml_in_place(out.path)


def sparse_clone(config: Config, github_pat: str) -> None:
    """Init + sparse-fetch only .sops.yaml and the rotations' sops_files into cwd."""
    remote = f"https://x-access-token:{github_pat}@github.com/{config.github_repo}.git"
    sparse_paths = [config.sops_config, *(str(r.sops_file) for r in config.rotations)]
    sparse_paths += [str(r.k8s_secret.path) for r in config.rotations if r.k8s_secret]
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
            if rotation.k8s_secret:
                subprocess.run(["git", "add", "--", str(rotation.k8s_secret.path)], check=True)
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
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.model_validate(yaml.safe_load(config_path.read_text()))
    github_pat = github_pat_file.read_text().strip()

    repo_dir = Path("/tmp/repo")
    repo_dir.mkdir()
    os.chdir(repo_dir)
    sparse_clone(config, github_pat)

    # Isolate entries: one broken rotation must not starve the others (a bad
    # entry otherwise stalls every token in the cluster until someone notices).
    # Failures still fail the Job at the end, after healthy entries commit.
    rotated: list[str] = []
    failed: list[str] = []
    with httpx.Client(timeout=30) as client:
        for rotation in config.rotations:
            try:
                if rotate_one(client, rotation, config):
                    rotated.append(rotation.name)
            except Exception:
                logger.exception("%s: rotation failed; continuing with remaining entries", rotation.name)
                failed.append(rotation.name)

    if rotated:
        commit_and_push(config, rotated)
    else:
        logger.info("no rotations needed this cycle")
    if failed:
        raise SystemExit(f"rotations failed: {', '.join(failed)}")


if __name__ == "__main__":
    typer.run(main)
