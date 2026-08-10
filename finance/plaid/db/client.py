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
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.institutions_get_by_id_request_options import InstitutionsGetByIdRequestOptions
from plaid.model.institutions_search_request import InstitutionsSearchRequest
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

from finance.plaid.db.products import Product

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


@dataclass(frozen=True)
class InstitutionSummary:
    institution_id: str
    name: str


@dataclass(frozen=True)
class InstitutionDetail:
    institution_id: str
    name: str
    # Everything Plaid says this institution offers, including products this app cannot sync. The
    # caller narrows it; the raw list is kept so the UI can say "supported but not synced here"
    # rather than silently omitting.
    products: list[str]
    url: str | None


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
    def institutions_search(self, request: InstitutionsSearchRequest, /) -> DictResponse: ...
    def institutions_get_by_id(self, request: InstitutionsGetByIdRequest, /) -> DictResponse: ...


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

    def search_institutions(self, query: str, *, count: int = 10) -> list[InstitutionSummary]:
        """Institutions matching a typeahead query, unfiltered by product.

        Plaid's `products` filter would return only institutions supporting *all* of them, which
        inverts the flow this serves: the user names their bank, and the products follow from it.
        """
        # `products` and `options` are omitted rather than passed as None: the generated SDK
        # type-checks every kwarg it receives and rejects None for both.
        request = InstitutionsSearchRequest(query=query, country_codes=[CountryCode("US")])
        try:
            response = self._api.institutions_search(request).to_dict()
        except PlaidApiException as exc:
            raise _plaid_api_error("/institutions/search", exc) from exc
        institutions = cast(list[dict[str, object]], response.get("institutions") or [])
        return [
            InstitutionSummary(institution_id=str(item["institution_id"]), name=str(item["name"]))
            for item in institutions[:count]
        ]

    def get_institution(self, institution_id: str) -> InstitutionDetail:
        request = InstitutionsGetByIdRequest(
            institution_id=institution_id,
            country_codes=[CountryCode("US")],
            options=InstitutionsGetByIdRequestOptions(include_optional_metadata=True),
        )
        try:
            response = self._api.institutions_get_by_id(request).to_dict()
        except PlaidApiException as exc:
            raise _plaid_api_error("/institutions/get_by_id", exc) from exc
        institution = cast(dict[str, object], response["institution"])
        url = institution.get("url")
        return InstitutionDetail(
            institution_id=str(institution["institution_id"]),
            name=str(institution["name"]),
            products=[str(product) for product in cast(list[object], institution.get("products") or [])],
            url=str(url) if url else None,
        )

    def create_link_token(
        self,
        *,
        products: list[str],
        redirect_uri: str,
        client_user_id: str,
        transaction_days_requested: int = 730,
        client_name: str = "Plaid MCP",
    ) -> LinkTokenResult:
        """Create a Link token for an explicit product set.

        Deliberately does NOT pin the institution. `LinkTokenCreateRequest` carries an
        `institution_id` attribute and Plaid rejects it with `INVALID_INSTITUTION` — it is not a
        documented request field for /link/token/create, only a response and webhook one. The
        typeahead still decides *which products* to request; Link picks the institution.

        Only ONE product goes in `products`; the rest go in `required_if_supported_products`.
        `products` is a hard requirement checked against the *accounts the user selects*, not just
        the institution — so requesting liabilities at a brokerage that offers them institution-wide
        but has no loan or card account fails the Link *after* the user has already consented at
        their bank. `required_if_supported_products` activates per selected account and never fails
        the flow, so a product that turns out not to apply is simply skipped.
        """
        anchor, conditional = _split_link_products(products)
        request_args: dict[str, object] = {
            "client_name": client_name,
            "user": LinkTokenCreateRequestUser(client_user_id=client_user_id),
            "products": [Products(anchor)],
            "country_codes": [CountryCode("US")],
            "language": "en",
            "redirect_uri": redirect_uri,
        }
        if conditional:
            request_args["required_if_supported_products"] = [Products(product) for product in conditional]
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


def _split_link_products(products: list[str]) -> tuple[str, list[str]]:
    """Split a requested set into the one hard-required product and the conditional rest.

    The anchor is the earliest in `Product` declaration order, which is also least-to-most likely to
    have no eligible account: nearly every account supports transactions, investments needs a
    brokerage, liabilities needs a loan or card. Anchoring on the broadest keeps the one product
    that CAN fail the Link as the one least likely to.
    """
    if not products:
        raise ValueError("a Link token needs at least one product")
    ordered = [p.value for p in Product if p.value in set(products)]
    unknown = sorted(set(products) - {p.value for p in Product})
    if unknown:
        raise ValueError(f"not products this app syncs: {unknown}")
    return ordered[0], ordered[1:]


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
