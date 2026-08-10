from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import cast

import pytest_bazel
from fastapi.testclient import TestClient
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.transactions_get_request import TransactionsGetRequest

from finance.plaid.db.client import PlaidClient, PlaidSdkApiLike
from finance.plaid.db.config import PlaidWebSettings
from finance.plaid.db.link_profiles import LinkProfile
from finance.plaid.db.link_store import PlaidLinkStorage, StoredLink
from finance.plaid.link.app import PlaidWebClient, create_app


class _FakeStorage:
    def __init__(self) -> None:
        self.purged_item_ids: list[str] = []

    def _link(self) -> StoredLink:
        return StoredLink(
            item_id="item_123",
            label="Chase personal",
            institution_id="ins_3",
            institution_name="Chase",
            link_profile=LinkProfile.CREDIT_CARD_DETAIL,
            products_requested=["transactions", "liabilities"],
            transaction_days_requested=90,
            products_authorized=["transactions"],
            products_billed=[],
            status="active",
            access_token_secret="plaid-item-123-access-token",
            last_synced_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
            earliest_transaction_date=date(2026, 3, 2),
            latest_transaction_date=date(2026, 5, 30),
            synced_transaction_count=42,
        )

    async def list_active_links(self) -> list[StoredLink]:
        return [self._link()]

    async def get_link(self, item_id: str) -> StoredLink | None:
        return self._link() if item_id == "item_123" else None

    async def purge_link_data(self, item_id: str) -> None:
        self.purged_item_ids.append(item_id)


class _FakeSecrets:
    def __init__(self) -> None:
        self.deleted_secret_names: list[str] = []

    async def read_access_token(self, secret_name: str) -> str:
        if secret_name != "plaid-item-123-access-token":
            raise AssertionError(f"unexpected secret read in smoke test: {secret_name}")
        return "access-sandbox-existing"

    async def write_access_token(self, secret_name: str, access_token: str) -> None:
        raise AssertionError(f"unexpected secret write in smoke test: {secret_name}")

    async def delete_access_token(self, secret_name: str) -> None:
        self.deleted_secret_names.append(secret_name)


class _FakePlaidApi:
    api_client = object()

    def __init__(self) -> None:
        self.link_token_requests: list[dict[str, object]] = []
        self.exchanged_public_tokens: list[str] = []
        self.removed_access_tokens: list[str] = []

    def link_token_create(self, request: LinkTokenCreateRequest) -> SimpleNamespace:
        self.link_token_requests.append(request.to_dict())
        return SimpleNamespace(link_token=f"link-token-{len(self.link_token_requests)}")

    def item_public_token_exchange(self, request: ItemPublicTokenExchangeRequest) -> SimpleNamespace:
        self.exchanged_public_tokens.append(request.public_token)
        return SimpleNamespace(access_token="access-sandbox-new", item_id="item-sandbox-new")

    def item_remove(self, request: ItemRemoveRequest) -> object:
        self.removed_access_tokens.append(request.access_token)
        return object()

    def item_get(self, request: ItemGetRequest) -> object:
        raise AssertionError("unexpected sync call in smoke test")

    def accounts_get(self, request: AccountsGetRequest) -> object:
        raise AssertionError("unexpected sync call in smoke test")

    def transactions_get(self, request: TransactionsGetRequest) -> object:
        raise AssertionError("unexpected sync call in smoke test")

    def investments_holdings_get(self, request: InvestmentsHoldingsGetRequest) -> object:
        raise AssertionError("unexpected sync call in smoke test")

    def investments_transactions_get(self, request: InvestmentsTransactionsGetRequest) -> object:
        raise AssertionError("unexpected sync call in smoke test")

    def liabilities_get(self, request: LiabilitiesGetRequest) -> object:
        raise AssertionError("unexpected sync call in smoke test")


def _client(
    *, storage: _FakeStorage | None = None, secrets: _FakeSecrets | None = None, api: _FakePlaidApi | None = None
) -> TestClient:
    settings = PlaidWebSettings(
        plaid_env="sandbox",
        client_id="client-id",
        client_secret="client-secret",
        DATABASE_URL="postgresql://example.invalid/plaid",
        target_namespace="plaid-mcp",
    )
    return TestClient(
        create_app(
            settings,
            storage=cast(PlaidLinkStorage, storage or _FakeStorage()),
            secrets=secrets or _FakeSecrets(),
            client=cast(PlaidWebClient, PlaidClient(api=cast(PlaidSdkApiLike, api or _FakePlaidApi()))),
        )
    )


def test_link_ui_exposes_management_actions() -> None:
    with _client() as client:
        response = client.get("/link")
        root_response = client.get("/")

    assert response.status_code == 200
    assert root_response.status_code == 200
    assert "Connect Institution" in response.text
    assert "Connect Institution" in root_response.text
    assert "History days" in response.text
    assert "Active Links" in response.text


def test_static_assets_are_served_with_their_own_content_types() -> None:
    """The page references /static/link.{css,js} by absolute path; if those routes break, the UI
    renders unstyled and inert rather than failing visibly."""
    with _client() as client:
        page = client.get("/link")
        css = client.get("/static/link.css")
        js = client.get("/static/link.js")

    assert '<link rel="stylesheet" href="/static/link.css" />' in page.text
    assert '<script src="/static/link.js"></script>' in page.text
    assert css.headers["content-type"].startswith("text/css")
    assert js.headers["content-type"].startswith("text/javascript")
    # The per-link row actions are rendered by the script, not present in the served HTML.
    for action in ("Add scopes", "Repair", "Sync", "Remove"):
        assert action in js.text


def test_list_links_exposes_product_and_secret_state() -> None:
    with _client() as client:
        response = client.get("/api/links")

    expected_observed_days = (datetime.now(UTC).date() - date(2026, 3, 2)).days
    assert response.status_code == 200
    assert response.json() == [
        {
            "item_id": "item_123",
            "label": "Chase personal",
            "institution_id": "ins_3",
            "institution_name": "Chase",
            "link_profile": "credit_card_detail",
            "products_requested": ["transactions", "liabilities"],
            "transaction_days_requested": 90,
            "earliest_transaction_date": "2026-03-02",
            "latest_transaction_date": "2026-05-30",
            "observed_transaction_history_days": expected_observed_days,
            "synced_transaction_count": 42,
            "products_authorized": ["transactions"],
            "products_billed": [],
            "status": "active",
            "access_token_secret": "plaid-item-123-access-token",
            "last_synced_at": "2026-05-31T12:00:00+00:00",
        }
    ]


def test_get_link_state_returns_single_link() -> None:
    with _client() as client:
        response = client.get("/api/links/item_123")

    assert response.status_code == 200
    assert response.json()["item_id"] == "item_123"


def test_get_link_state_unknown_item_returns_404() -> None:
    with _client() as client:
        response = client.get("/api/links/nope")

    assert response.status_code == 404


def test_web_config_exposes_default_history_depth_and_the_profile_catalog() -> None:
    with _client() as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert body["transaction_days"] == 730
    assert body["max_transaction_days"] == 730
    # The dropdown is built from this, so a profile the backend knows must reach the UI with the
    # products it actually requests attached.
    assert [entry["value"] for entry in body["profiles"]] == [p.value for p in LinkProfile]
    by_value = {entry["value"]: entry for entry in body["profiles"]}
    assert by_value["credit_card_detail"]["products"] == ["transactions", "liabilities"]
    assert by_value["advanced"]["products"] == []


def test_create_link_token_initializes_requested_products() -> None:
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    result = client.create_link_token(
        profile=LinkProfile.CREDIT_CARD_DETAIL,
        redirect_uri="https://example.test/link/callback",
        client_user_id="owner",
    )

    assert result.link_token == "link-token-1"
    assert result.products == ["transactions", "liabilities"]
    assert result.transaction_days_requested == 730
    assert api.link_token_requests == [
        {
            "client_name": "Plaid MCP",
            "country_codes": ["US"],
            "language": "en",
            "products": ["transactions", "liabilities"],
            "redirect_uri": "https://example.test/link/callback",
            "transactions": {"days_requested": 730},
            "user": {"client_user_id": "owner"},
        }
    ]


def test_create_link_token_allows_custom_transaction_history_depth() -> None:
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    result = client.create_link_token(
        profile=LinkProfile.CASHFLOW,
        redirect_uri="https://example.test/link/callback",
        client_user_id="owner",
        transaction_days_requested=180,
    )

    assert result.transaction_days_requested == 180
    assert api.link_token_requests[0]["transactions"] == {"days_requested": 180}


def test_create_link_token_omits_transactions_config_without_transactions_product() -> None:
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    result = client.create_link_token(
        profile=LinkProfile.INVESTMENTS_HOLDINGS,
        redirect_uri="https://example.test/link/callback",
        client_user_id="owner",
    )

    assert result.products == ["investments"]
    assert result.transaction_days_requested is None
    assert "transactions" not in api.link_token_requests[0]


def test_create_update_link_token_requests_additional_consented_products_only() -> None:
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    result = client.create_update_link_token(
        access_token="access-sandbox-existing",
        redirect_uri="https://example.test/link/callback",
        client_user_id="owner",
        additional_products=["investments"],
    )

    assert result.link_token == "link-token-1"
    assert result.products == ["investments"]
    assert api.link_token_requests == [
        {
            "access_token": "access-sandbox-existing",
            "additional_consented_products": ["investments"],
            "client_name": "Plaid MCP",
            "country_codes": ["US"],
            "language": "en",
            "redirect_uri": "https://example.test/link/callback",
            "user": {"client_user_id": "owner"},
        }
    ]
    assert "products" not in api.link_token_requests[0]
    assert "transactions" not in api.link_token_requests[0]


def test_exchange_public_token_uses_sdk_request() -> None:
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    result = client.exchange_public_token("public-sandbox-token")

    assert api.exchanged_public_tokens == ["public-sandbox-token"]
    assert result.access_token == "access-sandbox-new"
    assert result.item_id == "item-sandbox-new"


def test_remove_item_uses_sdk_request() -> None:
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    client.remove_item("access-sandbox-existing")

    assert api.removed_access_tokens == ["access-sandbox-existing"]


def test_remove_link_purges_mirrored_link_data_after_plaid_removal() -> None:
    api = _FakePlaidApi()
    storage = _FakeStorage()
    secrets = _FakeSecrets()

    with _client(storage=storage, secrets=secrets, api=api) as client:
        response = client.post("/api/links/item_123/remove")

    assert response.status_code == 200
    assert response.json() == {"status": "removed"}
    assert api.removed_access_tokens == ["access-sandbox-existing"]
    assert secrets.deleted_secret_names == ["plaid-item-123-access-token"]
    assert storage.purged_item_ids == ["item_123"]


if __name__ == "__main__":
    pytest_bazel.main()
