"""Minimal Plaid client for the Plaid MCP server and experiments.

Reuses `airlock.oauth.provider.PlaidProvider` for link_token creation and
public_token exchange. Adds the data endpoints PlaidProvider doesn't cover
(`/sandbox/public_token/create`, `/accounts/get`, `/accounts/balance/get`,
`/transactions/get`, `/transactions/sync`, `/liabilities/get`).

Data endpoints parse Plaid JSON into typed models from `plaid.models` at the
boundary; callers get typed objects, not raw dicts. Credentials are passed in as a
`PlaidCreds`; loading them from sops/env lives in `plaid.dev_creds` (dev-only) so the
MCP server never bundles that machinery.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from airlock.oauth.provider import PlaidProvider, PlaidProviderConfig, TokenSecretConfig
from plaid.models import AccountsGetResponse, LiabilitiesGetResponse, TransactionsGetResponse

PLAID_HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}


@dataclass(frozen=True)
class PlaidCreds:
    client_id: str
    secret: str
    env: str

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


class PlaidAPIError(RuntimeError):
    """A Plaid API error response (HTTP 4xx/5xx with a Plaid error body).

    Carries Plaid's `error_type`/`error_code`/`error_message`/`request_id` so callers
    (e.g. the MCP server) can surface actionable faults like `PRODUCT_NOT_READY`,
    `RATE_LIMIT_EXCEEDED`, or `ITEM_LOGIN_REQUIRED`.
    """

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.error_type = payload.get("error_type")
        self.error_code = payload.get("error_code")
        self.error_message = payload.get("error_message") or payload.get("display_message")
        self.request_id = payload.get("request_id")
        detail = self.error_message or payload
        super().__init__(f"Plaid {status_code} {self.error_code or 'error'}: {detail}")


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
                try:
                    payload = r.json()
                except ValueError:
                    payload = {"error_message": r.text}
                raise PlaidAPIError(r.status_code, payload)
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

    async def accounts_get(self, access_token: str) -> AccountsGetResponse:
        return AccountsGetResponse.model_validate(await self._post("/accounts/get", {"access_token": access_token}))

    async def accounts_balance_get(
        self, access_token: str, account_ids: list[str] | None = None
    ) -> AccountsGetResponse:
        """Real-time balances (uncached; hits the institution). Heavily rate-limited per Item."""
        body: dict[str, Any] = {"access_token": access_token}
        if account_ids is not None:
            body["options"] = {"account_ids": account_ids}
        return AccountsGetResponse.model_validate(await self._post("/accounts/balance/get", body))

    async def transactions_get(
        self,
        access_token: str,
        start_date: str,
        end_date: str,
        account_ids: list[str] | None = None,
        offset: int = 0,
        count: int = 50,
    ) -> TransactionsGetResponse:
        """Date-range transactions with offset/count pagination (`total_transactions` is the full count)."""
        options: dict[str, Any] = {"offset": offset, "count": count}
        if account_ids is not None:
            options["account_ids"] = account_ids
        body = {"access_token": access_token, "start_date": start_date, "end_date": end_date, "options": options}
        return TransactionsGetResponse.model_validate(await self._post("/transactions/get", body))

    async def liabilities_get(self, access_token: str) -> LiabilitiesGetResponse:
        return LiabilitiesGetResponse.model_validate(
            await self._post("/liabilities/get", {"access_token": access_token})
        )

    async def transactions_sync(self, access_token: str, cursor: str = "", count: int = 500) -> dict[str, Any]:
        """Incremental cursor sync. Used by the sandbox smoke test; not on the MCP surface."""
        return await self._post("/transactions/sync", {"access_token": access_token, "cursor": cursor, "count": count})
