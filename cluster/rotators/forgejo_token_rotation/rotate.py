"""Rotate Forgejo API tokens into SOPS files and tea config Secrets.

The rotator authenticates to Forgejo with each agent account's username/password,
mints a scoped API token for the full account (no repository restriction), writes
the raw token to a SOPS file, and optionally writes a Kubernetes Secret containing
both the raw token and a ready-to-mount `tea` config.

Token minting is an external side effect, so pruning old rotator-created tokens is
deliberately deferred until after the new SOPS outputs have been committed and
pushed. If the commit/push fails, the new token can sit unused until a later run;
the old token is left intact.
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import httpx
import typer
import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

FULL_ACCOUNT_SCOPES = [
    "write:activitypub",
    "write:issue",
    "write:misc",
    "write:notification",
    "write:organization",
    "write:package",
    "write:repository",
    "write:user",
]


class TeaSecretOutput(BaseModel):
    """Optional k8s Secret manifest carrying a mounted `tea` config."""

    path: Path = Field(description="Repo-relative path for the Secret manifest (under cluster/k8s/, *.sops.yaml)")
    name: str
    namespace: str
    config_key: str = "config.yml"
    token_key: str = "token"
    username_key: str = "username"
    url_key: str = "url"
    token_name_key: str = "token-name"
    token_last_eight_key: str = "token-last-eight"


class Rotation(BaseModel):
    name: str = Field(description="Human-readable name for logs and commit messages")
    credentials_dir: Path = Field(description="Mounted secret dir with username/password and optional url/internal_url")
    sops_file: Path = Field(description="Repo-relative path to the encrypted raw-token output")
    token_field: str = Field(default="token", description="YAML field name for the raw token in sops_file")
    token_prefix: str | None = Field(
        default=None,
        description="Prefix for Forgejo token names. Defaults to rotation.name; minted names append UTC timestamp.",
    )
    scopes: list[str] = Field(
        default_factory=lambda: list(FULL_ACCOUNT_SCOPES),
        description="Forgejo token scopes. Defaults to every non-admin write scope.",
    )
    rotate_after_days: int = Field(default=30, description="Mint a fresh token once the current one is this old")
    keep_previous: int = Field(
        default=1,
        description="After committing a new token, keep this many older rotator-created tokens as rollback cushion",
    )
    api_url: str = Field(
        default="http://forgejo-http.forgejo:3000",
        description="Forgejo base URL used by the rotator API client; credentials_dir/internal_url overrides it",
    )
    tea_url: str = Field(
        default="https://git.allegedly.works",
        description="Forgejo URL written into tea config; credentials_dir/url overrides it",
    )
    login_name: str = Field(default="forgejo", description="tea login name")
    tea_secret: TeaSecretOutput | None = Field(
        default=None, description="When set, also write a k8s Secret manifest with config.yml + raw token"
    )

    @property
    def token_name_prefix(self) -> str:
        return self.token_prefix or self.name


class Config(BaseModel):
    rotations: list[Rotation]
    github_repo: str = "agentydragon/ducktape"
    sops_config: str = Field(default=".sops.yaml", description="Repo path sops reads to pick the recipient set")
    git_author_name: str = "forgejo-token-rotation"
    git_author_email: str = "noreply@allegedly.works"


@dataclass(frozen=True)
class ForgejoCredentials:
    username: str
    password: str
    api_url: str
    tea_url: str


@dataclass(frozen=True)
class RotatedToken:
    rotation: Rotation
    credentials: ForgejoCredentials
    token_id: int | None
    token_name: str
    token_last_eight: str


def read_credentials(rotation: Rotation) -> ForgejoCredentials:
    username = (rotation.credentials_dir / "username").read_text().strip()
    password = (rotation.credentials_dir / "password").read_text().strip()
    api_url = (
        (rotation.credentials_dir / "internal_url").read_text().strip()
        if (rotation.credentials_dir / "internal_url").exists()
        else rotation.api_url
    )
    tea_url = (
        (rotation.credentials_dir / "url").read_text().strip()
        if (rotation.credentials_dir / "url").exists()
        else rotation.tea_url
    )
    return ForgejoCredentials(
        username=username, password=password, api_url=api_url.rstrip("/"), tea_url=tea_url.rstrip("/")
    )


def api_base(api_url: str) -> str:
    return f"{api_url.rstrip('/')}/api/v1"


def unencrypted_stamps(sops_file: Path) -> dict[str, Any]:
    """Read plaintext `*_unencrypted` stamps from a SOPS YAML file."""
    if not sops_file.exists():
        return {}
    return yaml.safe_load(sops_file.read_text()) or {}


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def token_still_present(stamps: dict[str, Any], tokens: list[dict[str, Any]]) -> bool:
    token_id = stamps.get("token_id_unencrypted")
    token_name = stamps.get("token_name_unencrypted")
    token_last_eight = stamps.get("token_last_eight_unencrypted")
    if token_id is None and token_name is None:
        return False

    for token in tokens:
        id_matches = token_id is not None and str(token.get("id")) == str(token_id)
        name_matches = token_name is not None and token.get("name") == token_name
        if not (id_matches or name_matches):
            continue
        if token_last_eight is not None and token.get("token_last_eight") != token_last_eight:
            continue
        return True
    return False


def should_rotate(
    rotation: Rotation, stamps: dict[str, Any], tokens: list[dict[str, Any]], now: datetime | None = None
) -> tuple[bool, str]:
    """Return whether a rotation is due and the reason."""
    now = now or datetime.now(UTC)
    if not stamps:
        return True, "no existing token stamp"
    if stamps.get("scopes_unencrypted") != rotation.scopes:
        return True, "scope set changed"
    if stamps.get("repository_access_unencrypted") != "all":
        return True, "repository access stamp missing or changed"
    if int(stamps.get("rotate_after_days_unencrypted", 0)) != rotation.rotate_after_days:
        return True, "rotation interval changed"
    if not token_still_present(stamps, tokens):
        return True, "stamped token is not present in Forgejo"

    rotated_at = stamps.get("rotated_at_unencrypted")
    if not rotated_at:
        return True, "rotated_at stamp missing"
    next_rotation = parse_utc(rotated_at) + timedelta(days=rotation.rotate_after_days)
    if now >= next_rotation:
        return True, f"token age reached {rotation.rotate_after_days}d"
    return False, f"fresh until {next_rotation:%Y-%m-%dT%H:%M:%SZ}"


def list_tokens(client: httpx.Client, creds: ForgejoCredentials) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = client.get(
            f"{api_base(creds.api_url)}/users/{creds.username}/tokens",
            auth=(creds.username, creds.password),
            params={"page": page, "limit": 50},
        )
        resp.raise_for_status()
        batch: list[dict[str, Any]] = resp.json()
        tokens.extend(batch)
        if len(batch) < 50:
            return tokens
        page += 1


def mint_token(
    client: httpx.Client, rotation: Rotation, creds: ForgejoCredentials, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    token_name = f"{rotation.token_name_prefix}-{now:%Y%m%d%H%M%S}{now.microsecond:06d}"
    # Omit `repositories`: in Forgejo 15's API that means the token is not
    # limited to selected repositories, i.e. it follows the account's full access.
    resp = client.post(
        f"{api_base(creds.api_url)}/users/{creds.username}/tokens",
        auth=(creds.username, creds.password),
        json={"name": token_name, "scopes": rotation.scopes},
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    token = data.get("sha1") or data.get("token")
    if not token:
        raise RuntimeError(f"{rotation.name}: Forgejo returned no token body field")
    data["sha1"] = token
    data["token_last_eight"] = data.get("token_last_eight") or token[-8:]
    return data


def verify_token(client: httpx.Client, creds: ForgejoCredentials, token: str) -> None:
    resp = client.get(f"{api_base(creds.api_url)}/user", headers={"Authorization": f"token {token}"})
    resp.raise_for_status()
    login = resp.json().get("login")
    if login != creds.username:
        raise RuntimeError(f"minted token authenticates as {login!r}, expected {creds.username!r}")


def tea_config_yaml(rotation: Rotation, creds: ForgejoCredentials, token: str) -> str:
    return yaml.safe_dump(
        {
            "logins": [
                {
                    "name": rotation.login_name,
                    "url": creds.tea_url,
                    "token": token,
                    "default": True,
                    "version_check": False,
                    "user": creds.username,
                }
            ]
        },
        sort_keys=False,
        width=2**31,
    )


def write_raw_token_sops_file(
    rotation: Rotation, creds: ForgejoCredentials, token_data: dict[str, Any], now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    token = token_data["sha1"]
    stamps: dict[str, Any] = {
        "rotated_at_unencrypted": f"{now:%Y-%m-%dT%H:%M:%SZ}",
        "rotate_after_days_unencrypted": rotation.rotate_after_days,
        "repository_access_unencrypted": "all",
        "scopes_unencrypted": rotation.scopes,
        "token_id_unencrypted": token_data.get("id"),
        "token_name_unencrypted": token_data["name"],
        "token_last_eight_unencrypted": token_data.get("token_last_eight") or token[-8:],
        "username": creds.username,
        "url": creds.tea_url,
        rotation.token_field: token,
    }
    rotation.sops_file.parent.mkdir(parents=True, exist_ok=True)
    rotation.sops_file.write_text(yaml.safe_dump(stamps, sort_keys=False, width=2**31))
    subprocess.run(["sops", "encrypt", "--in-place", str(rotation.sops_file)], check=True)


def build_secret_manifest(
    out: TeaSecretOutput, rotation: Rotation, creds: ForgejoCredentials, token_data: dict[str, Any]
) -> dict[str, Any]:
    token = token_data["sha1"]
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": out.name,
            "namespace": out.namespace,
            "annotations": {"description": "Forgejo API token + tea config minted by forgejo-token-rotation."},
        },
        "type": "Opaque",
        "stringData": {
            out.config_key: tea_config_yaml(rotation, creds, token),
            out.token_key: token,
            out.username_key: creds.username,
            out.url_key: creds.tea_url,
            out.token_name_key: token_data["name"],
            out.token_last_eight_key: token_data.get("token_last_eight") or token[-8:],
        },
    }


def write_tea_secret(rotation: Rotation, creds: ForgejoCredentials, token_data: dict[str, Any]) -> None:
    if rotation.tea_secret is None:
        return
    rotation.tea_secret.path.parent.mkdir(parents=True, exist_ok=True)
    rotation.tea_secret.path.write_text(
        yaml.safe_dump(
            build_secret_manifest(rotation.tea_secret, rotation, creds, token_data), sort_keys=False, width=2**31
        )
    )
    subprocess.run(["sops", "encrypt", "--in-place", str(rotation.tea_secret.path)], check=True)


def rotate_one(client: httpx.Client, rotation: Rotation) -> RotatedToken | None:
    creds = read_credentials(rotation)
    tokens = list_tokens(client, creds)
    due, reason = should_rotate(rotation, unencrypted_stamps(rotation.sops_file), tokens)
    if not due:
        logger.info("%s: %s; skipping", rotation.name, reason)
        return None

    now = datetime.now(UTC)
    logger.info("%s: rotating (%s)", rotation.name, reason)
    token_data = mint_token(client, rotation, creds, now)
    verify_token(client, creds, token_data["sha1"])
    write_raw_token_sops_file(rotation, creds, token_data, now)
    write_tea_secret(rotation, creds, token_data)
    return RotatedToken(
        rotation=rotation,
        credentials=creds,
        token_id=token_data.get("id"),
        token_name=token_data["name"],
        token_last_eight=token_data.get("token_last_eight") or token_data["sha1"][-8:],
    )


def tokens_to_prune(rotation: Rotation, current: RotatedToken, tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prefix = f"{rotation.token_name_prefix}-"
    candidates = [
        token
        for token in tokens
        if str(token.get("name", "")).startswith(prefix)
        and str(token.get("id")) != str(current.token_id)
        and token.get("name") != current.token_name
    ]
    candidates.sort(key=lambda token: str(token.get("name", "")), reverse=True)
    return candidates[rotation.keep_previous :]


def delete_token(client: httpx.Client, creds: ForgejoCredentials, token: dict[str, Any]) -> None:
    token_ref = str(token.get("id") or token["name"])
    resp = client.delete(
        f"{api_base(creds.api_url)}/users/{creds.username}/tokens/{quote(token_ref, safe='')}",
        auth=(creds.username, creds.password),
    )
    if resp.status_code == 404:
        logger.info("%s: old token already absent", token_ref)
        return
    resp.raise_for_status()


def prune_old_tokens(client: httpx.Client, rotated: list[RotatedToken]) -> None:
    for result in rotated:
        tokens = list_tokens(client, result.credentials)
        for token in tokens_to_prune(result.rotation, result, tokens):
            logger.info("%s: pruning old token %s", result.rotation.name, token.get("name"))
            delete_token(client, result.credentials, token)


def sparse_clone(config: Config, github_pat: str) -> None:
    """Init + sparse-fetch only .sops.yaml and the configured outputs into cwd."""
    remote = f"https://x-access-token:{github_pat}@github.com/{config.github_repo}.git"
    sparse_paths = [config.sops_config, *(str(r.sops_file) for r in config.rotations)]
    sparse_paths += [str(r.tea_secret.path) for r in config.rotations if r.tea_secret]
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], check=True)
    subprocess.run(["git", "config", "core.sparseCheckout", "true"], check=True)
    Path(".git/info/sparse-checkout").write_text("\n".join(sparse_paths) + "\n")
    subprocess.run(["git", "fetch", "-q", "--depth=1", "--no-tags", "origin", "devel"], check=True)
    subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], check=True)


def commit_and_push(config: Config, rotated: list[RotatedToken]) -> bool:
    subprocess.run(["git", "config", "user.name", config.git_author_name], check=True)
    subprocess.run(["git", "config", "user.email", config.git_author_email], check=True)
    for result in rotated:
        subprocess.run(["git", "add", "--", str(result.rotation.sops_file)], check=True)
        if result.rotation.tea_secret:
            subprocess.run(["git", "add", "--", str(result.rotation.tea_secret.path)], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        logger.info("tokens minted but SOPS outputs are unchanged on disk; nothing to commit")
        return False
    message = f"chore: rotate forgejo tea tokens ({datetime.now(UTC):%Y-%m-%d}): "
    message += ", ".join(result.rotation.name for result in rotated)
    subprocess.run(["git", "commit", "-q", "-m", message], check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:devel"], check=True)
    logger.info("pushed: %s", ", ".join(result.rotation.name for result in rotated))
    return True


def main(
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False, help="tokens.yaml")],
    github_pat_file: Annotated[Path, typer.Option("--github-pat-file")] = Path("/var/run/secrets/github-pat/token"),
    ca_bundle: Annotated[Path, typer.Option(help="CA bundle assembled for git + httpx")] = Path("/tmp/ca-bundle.crt"),
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = Config.model_validate(yaml.safe_load(config_path.read_text()))
    github_pat = github_pat_file.read_text().strip()

    ca_bundle.write_text(
        "".join(p.read_text() for p in sorted(Path("/usr/share/ca-certificates/mozilla").glob("*.crt")))
    )
    os.environ["GIT_SSL_CAINFO"] = str(ca_bundle)

    repo_dir = Path("/tmp/repo")
    repo_dir.mkdir()
    os.chdir(repo_dir)
    sparse_clone(config, github_pat)

    rotated: list[RotatedToken] = []
    failed: list[str] = []
    with httpx.Client(verify=str(ca_bundle), timeout=30) as client:
        for rotation in config.rotations:
            try:
                result = rotate_one(client, rotation)
                if result is not None:
                    rotated.append(result)
            except Exception:
                logger.exception("%s: rotation failed; continuing with remaining entries", rotation.name)
                failed.append(rotation.name)

        if rotated:
            pushed = commit_and_push(config, rotated)
            if pushed:
                prune_old_tokens(client, rotated)
        else:
            logger.info("no rotations needed this cycle")

    if failed:
        raise SystemExit(f"rotations failed: {', '.join(failed)}")


if __name__ == "__main__":
    typer.run(main)
