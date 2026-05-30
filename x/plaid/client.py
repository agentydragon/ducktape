"""Minimal Plaid client for x/plaid experiments.

Reuses `airlock.oauth.provider.PlaidProvider` for link_token creation and
public_token exchange. Adds methods PlaidProvider doesn't cover: sandbox
public_token creation, /accounts/get, /transactions/sync.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from airlock.oauth.provider import PlaidProvider, PlaidProviderConfig, TokenSecretConfig

DEFAULT_SOPS_PATH = Path("secrets/plaid.sops.yaml")

PLAID_HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}


@dataclass(frozen=True)
class PlaidCreds:
    client_id: str
    secret: str
    env: str

    @classmethod
    def from_env(cls) -> "PlaidCreds":
        env = os.environ["PLAID_ENV"]
        if env not in PLAID_HOSTS:
            raise ValueError(f"PLAID_ENV must be one of {list(PLAID_HOSTS)}, got {env!r}")
        return cls(client_id=os.environ["PLAID_CLIENT_ID"], secret=os.environ["PLAID_SECRET"], env=env)

    @classmethod
    def from_sops(cls, env: str, path: Path = DEFAULT_SOPS_PATH) -> "PlaidCreds":
        if env not in PLAID_HOSTS:
            raise ValueError(f"env must be one of {list(PLAID_HOSTS)}, got {env!r}")
        plaintext = subprocess.run(["sops", "-d", str(path)], check=True, capture_output=True, text=True).stdout
        data = yaml.safe_load(plaintext)
        return cls(client_id=data["client_id"], secret=data["secrets"][env], env=env)

    @classmethod
    def load(cls) -> "PlaidCreds":
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
            return cls.from_sops(env, sops_path)
        return cls.from_env()

    @property
    def host(self) -> str:
        return PLAID_HOSTS[self.env]


def provider_for(creds: PlaidCreds, products: list[str], redirect_uri: str) -> PlaidProvider:
    config = PlaidProviderConfig(
        provider_type="plaid",
        name="plaid",
        display_name="Plaid",
        redirect_uri=redirect_uri,
        refresh_secret=TokenSecretConfig(name="unused"),
        access_secret=TokenSecretConfig(name="unused"),
        token_url=f"{creds.host}/item/public_token/exchange",
        products=products,
    )
    return PlaidProvider(config, creds.client_id, creds.secret)


class PlaidExtras:
    """Data-endpoint methods not on PlaidProvider."""

    def __init__(self, creds: PlaidCreds) -> None:
        self.creds = creds

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{self.creds.host}{path}",
                json={"client_id": self.creds.client_id, "secret": self.creds.secret, **body},
            )
            if r.is_error:
                raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text}")
            result: dict[str, Any] = r.json()
            return result

    async def sandbox_public_token_create(
        self, institution_id: str = "ins_109508", initial_products: list[str] | None = None
    ) -> str:
        data = await self._post(
            "/sandbox/public_token/create",
            {"institution_id": institution_id, "initial_products": initial_products or ["transactions"]},
        )
        return str(data["public_token"])

    async def accounts_get(self, access_token: str) -> dict[str, Any]:
        return await self._post("/accounts/get", {"access_token": access_token})

    async def transactions_sync(self, access_token: str, cursor: str = "", count: int = 500) -> dict[str, Any]:
        return await self._post("/transactions/sync", {"access_token": access_token, "cursor": cursor, "count": count})
