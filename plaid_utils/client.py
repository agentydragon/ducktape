"""Plaid client for the Plaid MCP server and experiments.

Thin wrapper over the official `plaid-python` SDK. The SDK is synchronous (urllib3, no
asyncio API), so this client is synchronous too — FastMCP runs the MCP tool functions in
a worker thread, so there's no event loop to block. Covers the read endpoints the MCP
server needs (`/accounts/get`, `/accounts/balance/get`, `/transactions/get`,
`/liabilities/get`) plus the sandbox helpers the smoke test uses
(`/sandbox/public_token/create`, `/item/public_token/exchange`, `/transactions/sync`).

Responses are run through the SDK's `sanitize_for_serialization` (dates -> ISO strings,
enums -> values, nested models -> dicts) and validated into the typed `plaid_utils.models`
Pydantic models at the boundary, so callers get typed objects, not raw dicts. Credentials
are passed in as `PlaidCreds`; loading them from sops/env lives in `plaid_utils.dev_creds`
(dev-only) so the MCP server never bundles that machinery.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import plaid
from plaid.api import plaid_api
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_balance_get_request_options import AccountsBalanceGetRequestOptions
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from plaid_utils.models import AccountsGetResponse, LiabilitiesGetResponse, TransactionsGetResponse

# Plaid removed the `development` environment in 2024; only sandbox/production remain.
PLAID_HOSTS = {"sandbox": plaid.Environment.Sandbox, "production": plaid.Environment.Production}


@dataclass(frozen=True)
class PlaidCreds:
    client_id: str
    secret: str
    env: str


class PlaidAPIError(RuntimeError):
    """A Plaid API error response, surfaced from the SDK's `ApiException`.

    Carries Plaid's `error_type`/`error_code`/`error_message`/`request_id` so callers
    (e.g. the MCP server) can surface actionable faults like `PRODUCT_NOT_READY`,
    `RATE_LIMIT_EXCEEDED`, or `ITEM_LOGIN_REQUIRED`.
    """

    def __init__(self, status: int | None, payload: dict[str, Any]) -> None:
        self.status_code = status
        self.error_type = payload.get("error_type")
        self.error_code = payload.get("error_code")
        self.error_message = payload.get("error_message") or payload.get("display_message")
        self.request_id = payload.get("request_id")
        detail = self.error_message or payload
        super().__init__(f"Plaid {status} {self.error_code or 'error'}: {detail}")

    @classmethod
    def from_api_exception(cls, exc: plaid.ApiException) -> PlaidAPIError:
        body = exc.body
        try:
            payload = json.loads(body) if body else {}
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {"error_message": str(body)}
        return cls(exc.status, payload)


class PlaidExtras:
    """Read endpoints + sandbox helpers over the plaid-python SDK."""

    def __init__(self, creds: PlaidCreds) -> None:
        configuration = plaid.Configuration(
            host=PLAID_HOSTS[creds.env], api_key={"clientId": creds.client_id, "secret": creds.secret}
        )
        self._client = plaid.ApiClient(configuration)
        self._api = plaid_api.PlaidApi(self._client)

    def _call(self, fn: Callable[[Any], Any], request: Any) -> Any:
        """Invoke a sync SDK call, map `ApiException` -> `PlaidAPIError`, JSON-sanitize the response."""
        try:
            response = fn(request)
        except plaid.ApiException as exc:
            raise PlaidAPIError.from_api_exception(exc) from exc
        return self._client.sanitize_for_serialization(response)

    def accounts_get(self, access_token: str) -> AccountsGetResponse:
        data = self._call(self._api.accounts_get, AccountsGetRequest(access_token=access_token))
        return AccountsGetResponse.model_validate(data)

    def accounts_balance_get(self, access_token: str, account_ids: list[str] | None = None) -> AccountsGetResponse:
        """Real-time balances (uncached; hits the institution). Heavily rate-limited per Item."""
        request = AccountsBalanceGetRequest(access_token=access_token)
        if account_ids is not None:
            request.options = AccountsBalanceGetRequestOptions(account_ids=account_ids)
        data = self._call(self._api.accounts_balance_get, request)
        return AccountsGetResponse.model_validate(data)

    def transactions_get(
        self,
        access_token: str,
        start_date: date,
        end_date: date,
        account_ids: list[str] | None = None,
        offset: int = 0,
        count: int = 50,
    ) -> TransactionsGetResponse:
        """Date-range transactions with offset/count pagination (`total_transactions` is the full count)."""
        options = TransactionsGetRequestOptions(offset=offset, count=count)
        if account_ids is not None:
            options.account_ids = account_ids
        request = TransactionsGetRequest(
            access_token=access_token, start_date=start_date, end_date=end_date, options=options
        )
        data = self._call(self._api.transactions_get, request)
        return TransactionsGetResponse.model_validate(data)

    def liabilities_get(self, access_token: str) -> LiabilitiesGetResponse:
        data = self._call(self._api.liabilities_get, LiabilitiesGetRequest(access_token=access_token))
        return LiabilitiesGetResponse.model_validate(data)

    def sandbox_public_token_create(
        self, institution_id: str = "ins_109508", initial_products: list[str] | None = None
    ) -> str:
        products = [Products(p) for p in (initial_products or ["transactions"])]
        request = SandboxPublicTokenCreateRequest(institution_id=institution_id, initial_products=products)
        data = self._call(self._api.sandbox_public_token_create, request)
        return str(data["public_token"])

    def exchange_public_token(self, public_token: str) -> str:
        """Exchange a public_token for a permanent access_token (`/item/public_token/exchange`)."""
        request = ItemPublicTokenExchangeRequest(public_token=public_token)
        data = self._call(self._api.item_public_token_exchange, request)
        return str(data["access_token"])

    def transactions_sync(self, access_token: str, cursor: str = "", count: int = 500) -> dict[str, Any]:
        """Incremental cursor sync. Used by the sandbox smoke test; not on the MCP surface."""
        request = TransactionsSyncRequest(access_token=access_token, count=count)
        if cursor:
            request.cursor = cursor
        data: dict[str, Any] = self._call(self._api.transactions_sync, request)
        return data
