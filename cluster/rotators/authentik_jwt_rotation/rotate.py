"""Rotate Authentik `client_credentials` JWTs into SOPS-encrypted files.

Validate final-token issuer and configured group, audience, and claim expectations.
Proxy-fronted rotations use a JWT-bearer exchange for a provider-scoped token.
Configured probes check the published Kubernetes token independently of expiry stamps.
Keep Git at the clone root so repo-relative SOPS creation rules resolve.
See `README.md` for the full rotation and deployment contract.
"""

import base64
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import yaml

from cluster.rotators.authentik_jwt_rotation.config import AUTH_BASE, Config, K8sSecretOutput, Probe, Rotation
from cluster.rotators.git_publish import enter_sparse_checkout, github_remote, push_outputs
from cluster.rotators.sops import write_sops_yaml
from cluster.rotators.sops_stamps import remaining_hours, unencrypted_stamps

logger = logging.getLogger(__name__)

TOKEN_URL = f"{AUTH_BASE}/application/o/token/"

K8S_API = "https://kubernetes.default.svc"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


# JWT claims are an untyped external payload; we read a handful of well-known keys.
def jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT's base64url payload (no signature verification)."""
    segment = token.split(".")[1]
    padded = segment + "=" * (-len(segment) % 4)
    payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return payload


def token_audiences(payload: dict[str, Any]) -> list[str]:
    """The `aud` claim normalized to a list (the JWT spec allows a string or a list)."""
    aud = payload.get("aud")
    if aud is None:
        return []
    return [aud] if isinstance(aud, str) else list(aud)


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
    stamps = unencrypted_stamps(rotation.sops_file)
    remaining = remaining_hours(stamps)
    fresh = remaining is not None and remaining > config.rotate_below_hours
    # An audience expectation the stored token doesn't already satisfy forces a
    # re-mint regardless of remaining validity — otherwise adding an audience
    # wouldn't take effect until the (~44-day) expiry. Read from the plaintext
    # `audiences_unencrypted` stamp (no SOPS decryption needed).
    if fresh and rotation.expected_audiences:
        stored = stamps.get("audiences_unencrypted")
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
        stored_claims = stamps.get("claims_unencrypted")
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
    write_sops_yaml(path=rotation.sops_file, document=stamps)
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
    write_sops_yaml(path=out.path, document=build_secret_manifest(out, token, exp_epoch))


def _output_paths(rotation: Rotation) -> list[Path]:
    return [rotation.sops_file, rotation.k8s_secret.path] if rotation.k8s_secret else [rotation.sops_file]


def main(
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False, help="rotations.yaml")],
    github_pat_file: Annotated[Path, typer.Option("--github-pat-file")] = Path("/var/run/secrets/github-pat/token"),
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.model_validate(yaml.safe_load(config_path.read_text()))
    github_pat = github_pat_file.read_text().strip()

    enter_sparse_checkout(
        Path("/tmp/repo"),
        github_remote(config.github_repo, github_pat),
        [config.sops_config, *(path for rotation in config.rotations for path in _output_paths(rotation))],
    )

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
        push_outputs(
            (path for rotation in config.rotations if rotation.name in rotated for path in _output_paths(rotation)),
            author_name=config.git_author_name,
            author_email=config.git_author_email,
            subject="authentik JWTs",
            names=rotated,
        )
    else:
        logger.info("no rotations needed this cycle")
    if failed:
        raise SystemExit(f"rotations failed: {', '.join(failed)}")


if __name__ == "__main__":
    typer.run(main)
