"""Lifecycle-aware Plaid client built on the official `plaid-python` SDK."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast

import certifi
import plaid
from plaid.api import plaid_api
from plaid.exceptions import ApiException as PlaidApiException
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from finance.plaid.db.link_profiles import LinkProfile, Product, products_for_profile

# Plaid removed the `development` environment in 2024; only sandbox/production remain.
PLAID_HOSTS = {"sandbox": plaid.Environment.Sandbox, "production": plaid.Environment.Production}
type JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class PlaidCreds:
    client_id: str
    secret: str
    env: str


@dataclass(frozen=True)
class LinkTokenResult:
    link_token: str
    products: list[str]
    transaction_days_requested: int | None


@dataclass(frozen=True)
class PublicTokenExchange:
    access_token: str
    item_id: str


class PlaidClientError(RuntimeError):
    def __init__(
        self, *, endpoint: str, status_code: int, text: str, payload: dict[str, JsonValue] | None = None
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.text = text
        self.payload = payload
        message = payload.get("error_message") if payload else text
        super().__init__(f"Plaid {endpoint} {status_code}: {message}")

    def public_detail(self) -> dict[str, JsonValue]:
        detail: dict[str, JsonValue] = {"endpoint": self.endpoint, "status_code": self.status_code}
        if self.payload:
            for key in (
                "error_type",
                "error_code",
                "error_message",
                "display_message",
                "documentation_url",
                "request_id",
            ):
                if key in self.payload:
                    detail[key] = self.payload[key]
        else:
            detail["error_message"] = self.text
        return detail


class LinkTokenCreateResponse(Protocol):
    link_token: str


class ItemPublicTokenExchangeResponse(Protocol):
    access_token: str
    item_id: str


class SandboxPublicTokenCreateResponse(Protocol):
    public_token: str


class DictResponse(Protocol):
    def to_dict(self) -> dict[str, object]: ...


class PlaidPoolManagerLike(Protocol):
    def clear(self) -> None: ...


class PlaidRestClientLike(Protocol):
    pool_manager: PlaidPoolManagerLike


class PlaidApiClientLike(Protocol):
    rest_client: PlaidRestClientLike

    def close(self) -> None: ...
    def sanitize_for_serialization(self, obj: object) -> object: ...


class PlaidSdkApiLike(Protocol):
    api_client: PlaidApiClientLike

    def link_token_create(self, request: LinkTokenCreateRequest, /) -> LinkTokenCreateResponse: ...
    def item_public_token_exchange(
        self, request: ItemPublicTokenExchangeRequest, /
    ) -> ItemPublicTokenExchangeResponse: ...
    def item_remove(self, request: ItemRemoveRequest, /) -> object: ...
    def item_get(self, request: ItemGetRequest, /) -> object: ...
    def accounts_get(self, request: AccountsGetRequest, /) -> DictResponse: ...
    def accounts_balance_get(self, request: AccountsBalanceGetRequest, /) -> object: ...
    def transactions_get(self, request: TransactionsGetRequest, /) -> object: ...
    def transactions_sync(self, request: TransactionsSyncRequest, /) -> DictResponse: ...
    def investments_holdings_get(self, request: InvestmentsHoldingsGetRequest, /) -> object: ...
    def investments_transactions_get(self, request: InvestmentsTransactionsGetRequest, /) -> object: ...
    def liabilities_get(self, request: LiabilitiesGetRequest, /) -> object: ...
    def sandbox_public_token_create(
        self, request: SandboxPublicTokenCreateRequest, /
    ) -> SandboxPublicTokenCreateResponse: ...


class PlaidClient:
    def __init__(self, creds: PlaidCreds | None = None, *, api: PlaidSdkApiLike | None = None) -> None:
        if api is None:
            if creds is None:
                raise ValueError("PlaidClient requires creds or api")
            api = _create_sdk_api(creds)
        self._api = api

    @property
    def api_client(self) -> PlaidApiClientLike:
        return self._api.api_client

    def close(self) -> None:
        self._api.api_client.close()
        self._api.api_client.rest_client.pool_manager.clear()

    def __enter__(self) -> PlaidClient:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def create_link_token(
        self,
        *,
        profile: LinkProfile,
        redirect_uri: str,
        client_user_id: str,
        advanced_products: list[str] | None = None,
        transaction_days_requested: int = 730,
        client_name: str = "Plaid MCP",
    ) -> LinkTokenResult:
        products = products_for_profile(profile, advanced_products)
        request_args: dict[str, object] = {
            "client_name": client_name,
            "user": LinkTokenCreateRequestUser(client_user_id=client_user_id),
            "products": [Products(product) for product in products],
            "country_codes": [CountryCode("US")],
            "language": "en",
            "redirect_uri": redirect_uri,
        }
        if Product.TRANSACTIONS.value in products:
            request_args["transactions"] = {"days_requested": transaction_days_requested}
        request = LinkTokenCreateRequest(**request_args)
        try:
            response = self._api.link_token_create(request)
        except PlaidApiException as exc:
            raise _plaid_api_error("/link/token/create", exc) from exc
        return LinkTokenResult(
            link_token=response.link_token,
            products=products,
            transaction_days_requested=transaction_days_requested if Product.TRANSACTIONS.value in products else None,
        )

    def create_update_link_token(
        self,
        *,
        access_token: str,
        redirect_uri: str,
        client_user_id: str,
        additional_products: list[str] | None = None,
        client_name: str = "Plaid MCP",
    ) -> LinkTokenResult:
        request = LinkTokenCreateRequest(
            client_name=client_name,
            user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
            country_codes=[CountryCode("US")],
            language="en",
            redirect_uri=redirect_uri,
            access_token=access_token,
        )
        if additional_products:
            request.additional_consented_products = [Products(product) for product in additional_products]
        try:
            response = self._api.link_token_create(request)
        except PlaidApiException as exc:
            raise _plaid_api_error("/link/token/create", exc) from exc
        return LinkTokenResult(
            link_token=response.link_token, products=additional_products or [], transaction_days_requested=None
        )

    def exchange_public_token(self, public_token: str) -> PublicTokenExchange:
        try:
            response = self._api.item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=public_token))
        except PlaidApiException as exc:
            raise _plaid_api_error("/item/public_token/exchange", exc) from exc
        return PublicTokenExchange(access_token=response.access_token, item_id=response.item_id)

    def remove_item(self, access_token: str) -> None:
        try:
            self._api.item_remove(ItemRemoveRequest(access_token=access_token))
        except PlaidApiException as exc:
            raise _plaid_api_error("/item/remove", exc) from exc

    def link_token_create(self, request: LinkTokenCreateRequest, /) -> LinkTokenCreateResponse:
        return self._api.link_token_create(request)

    def item_public_token_exchange(self, request: ItemPublicTokenExchangeRequest, /) -> ItemPublicTokenExchangeResponse:
        return self._api.item_public_token_exchange(request)

    def item_remove(self, request: ItemRemoveRequest, /) -> object:
        return self._api.item_remove(request)

    def item_get(self, request: ItemGetRequest, /) -> object:
        return self._api.item_get(request)

    def accounts_get(self, request: AccountsGetRequest, /) -> DictResponse:
        return self._api.accounts_get(request)

    def accounts_balance_get(self, request: AccountsBalanceGetRequest, /) -> object:
        return self._api.accounts_balance_get(request)

    def transactions_get(self, request: TransactionsGetRequest, /) -> object:
        return self._api.transactions_get(request)

    def transactions_sync(self, request: TransactionsSyncRequest, /) -> DictResponse:
        return self._api.transactions_sync(request)

    def investments_holdings_get(self, request: InvestmentsHoldingsGetRequest, /) -> object:
        return self._api.investments_holdings_get(request)

    def investments_transactions_get(self, request: InvestmentsTransactionsGetRequest, /) -> object:
        return self._api.investments_transactions_get(request)

    def liabilities_get(self, request: LiabilitiesGetRequest, /) -> object:
        return self._api.liabilities_get(request)

    def sandbox_public_token_create(
        self, request: SandboxPublicTokenCreateRequest, /
    ) -> SandboxPublicTokenCreateResponse:
        return self._api.sandbox_public_token_create(request)


def _create_sdk_api(creds: PlaidCreds) -> plaid_api.PlaidApi:
    configuration = plaid.Configuration(
        host=PLAID_HOSTS[creds.env], api_key={"clientId": creds.client_id, "secret": creds.secret}
    )
    # plaid-python (urllib3) passes ca_certs=ssl_ca_cert; left unset urllib3 falls back to the
    # system trust store, which the debian_slim runtime image ships empty -> production.plaid.com
    # fails with CERTIFICATE_VERIFY_FAILED. Point it at certifi's bundle (already in the image via
    # fastmcp -> httpx), matching how the other MCP servers get their CA roots.
    configuration.ssl_ca_cert = certifi.where()
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


def _plaid_api_error(endpoint: str, exc: PlaidApiException) -> PlaidClientError:
    text = str(exc.body or exc)
    payload = None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        payload = cast(dict[str, JsonValue], parsed)
    return PlaidClientError(endpoint=endpoint, status_code=exc.status or 500, text=text, payload=payload)
