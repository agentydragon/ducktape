"""Developer credential loading for plaid (sandbox smoke test, ad-hoc scripts).

Kept out of `plaid.client` so the MCP server's dependency path never bundles the
sops/subprocess machinery — the server constructs `PlaidCreds` from env settings.
"""

import os
import subprocess
from pathlib import Path

import yaml

from plaid_utils.client import PLAID_HOSTS, PlaidCreds

DEFAULT_SOPS_PATH = Path("secrets/plaid.sops.yaml")


def from_env() -> PlaidCreds:
    env = os.environ["PLAID_ENV"]
    if env not in PLAID_HOSTS:
        raise ValueError(f"PLAID_ENV must be one of {list(PLAID_HOSTS)}, got {env!r}")
    return PlaidCreds(client_id=os.environ["PLAID_CLIENT_ID"], secret=os.environ["PLAID_SECRET"], env=env)


def from_sops(env: str, path: Path = DEFAULT_SOPS_PATH) -> PlaidCreds:
    if env not in PLAID_HOSTS:
        raise ValueError(f"env must be one of {list(PLAID_HOSTS)}, got {env!r}")
    plaintext = subprocess.run(["sops", "-d", str(path)], check=True, capture_output=True, text=True).stdout
    data = yaml.safe_load(plaintext)
    return PlaidCreds(client_id=data["client_id"], secret=data["secrets"][env], env=env)


def load() -> PlaidCreds:
    """Prefer SOPS file (default `<workspace>/secrets/plaid.sops.yaml`); fall back to env vars."""
    env = os.environ["PLAID_ENV"]
    override = os.environ.get("PLAID_SECRETS_PATH")
    if override:
        sops_path = Path(override)
    else:
        # `bazel run` sets BUILD_WORKING_DIRECTORY to the invocation cwd.
        root = Path(os.environ.get("BUILD_WORKING_DIRECTORY", "."))
        sops_path = root / DEFAULT_SOPS_PATH
    if sops_path.exists():
        return from_sops(env, sops_path)
    return from_env()
