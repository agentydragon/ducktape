from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest_bazel
from fastapi.testclient import TestClient
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.institutions_search_request import InstitutionsSearchRequest
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
from finance.plaid.db.link_store import PlaidLinkStorage, StoredLink, SyncAlreadyRunningError
from finance.plaid.link.app import PlaidWebClient, create_app

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx


class _FakeStorage:
    def __init__(self) -> None:
        self.purged_item_ids: list[str] = []

    def _link(self) -> StoredLink:
        return StoredLink(
            item_id="item_123",
            label="Chase personal",
            institution_id="ins_3",
            institution_name="Chase",
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

    async def running_sync_item_ids(self) -> set[str]:
        return set()


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

    def institutions_search(self, request: InstitutionsSearchRequest) -> SimpleNamespace:
        return SimpleNamespace(to_dict=lambda: {"institutions": [{"institution_id": "ins_3", "name": "Chase"}]})

    def institutions_get_by_id(self, request: InstitutionsGetByIdRequest) -> SimpleNamespace:
        return SimpleNamespace(
            to_dict=lambda: {
                "institution": {
                    "institution_id": "ins_3",
                    "name": "Chase",
                    "url": "https://chase.example",
                    "products": ["auth", "transactions", "identity", "liabilities"],
                }
            }
        )


def _client(
    *, storage: _FakeStorage | None = None, secrets: _FakeSecrets | None = None, api: _FakePlaidApi | None = None
) -> TestClient:
    settings = PlaidWebSettings(
        plaid_env="sandbox",
        client_id="client-id",
        client_secret="client-secret",
        DATABASE_URL="postgresql://example.invalid/plaid",
        public_base_url="https://plaid-mcp.test",
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
    for action in ("Repair link", "Sync data", "Remove link"):
        assert action in js.text


def test_sync_conflict_is_a_sentence_not_an_item_id() -> None:
    """A link's own post-link sync runs for minutes, and clicking Sync during it hits this. It has
    to read as an explanation; the raw guard message is an opaque Plaid item id."""

    class _BusyStorage(_FakeStorage):
        async def running_sync_item_ids(self) -> set[str]:
            return {"item_123"}

        async def begin_sync_run(self, *, trigger: str, item_id: str | None, configured_windows: object) -> UUID:
            raise SyncAlreadyRunningError(str(item_id))

    with _client(storage=_BusyStorage()) as client:
        listed = client.get("/api/links").json()
        response = client.post("/api/links/item_123/sync")

    assert listed[0]["sync_running"] is True
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "SYNC_ALREADY_RUNNING"
    assert "already running" in response.json()["detail"]["error_message"]


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
            # Chase offers transactions + liabilities of what this app syncs; the Item is authorized
            # for transactions only, so liabilities is the one thing "Add ..." could still request.
            "addable_products": ["liabilities"],
            "sync_running": False,
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


def test_web_config_exposes_default_history_depth() -> None:
    with _client() as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    # The form renders one checkbox per entry, so this list is the single source for the control
    # set -- a Product missing here is a product the UI can never request.
    assert response.json() == {
        "transaction_days": 730,
        "max_transaction_days": 730,
        "products": ["transactions", "investments", "liabilities"],
    }


def test_institution_search_returns_typeahead_candidates() -> None:
    with _client() as client:
        response = client.get("/api/institutions", params={"q": "cha"})

    assert response.status_code == 200
    assert response.json() == [{"institution_id": "ins_3", "name": "Chase"}]


def test_institution_products_split_into_syncable_and_merely_offered() -> None:
    """The UI preselects what this app can mirror and names the rest, so a short checkbox list reads
    as a deliberate narrowing rather than an institution that offers little."""
    with _client() as client:
        response = client.get("/api/institutions/ins_3")

    assert response.status_code == 200
    assert response.json() == {
        "institution_id": "ins_3",
        "name": "Chase",
        "url": "https://chase.example",
        "syncable_products": ["transactions", "liabilities"],
        "unsupported_products": ["auth", "identity"],
    }


def test_link_token_never_pins_an_institution() -> None:
    """`institution_id` is not a documented request field for /link/token/create -- the generated
    SDK model carries the attribute, but Plaid answers INVALID_INSTITUTION. The typeahead decides
    which products to request; Link picks the institution."""
    api = _FakePlaidApi()
    with _client(api=api) as client:
        response = client.post("/api/link-token", json={"products": ["transactions", "liabilities"]})

    assert response.status_code == 200
    assert "institution_id" not in api.link_token_requests[0]
    assert api.link_token_requests[0]["products"] == ["transactions"]


def test_link_token_rejects_an_empty_product_set() -> None:
    with _client() as client:
        response = client.post("/api/link-token", json={"products": []})

    assert response.status_code == 422


def test_create_link_token_initializes_requested_products() -> None:
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    result = client.create_link_token(
        products=["transactions", "liabilities"],
        redirect_uri="https://example.test/link/callback",
        client_user_id="owner",
    )

    assert result.link_token == "link-token-1"
    assert result.products == ["transactions", "liabilities"]
    # Only transactions is hard-required; liabilities is conditional, so a selected account set with
    # no card or loan cannot fail the Link after the user has already consented at their bank.
    assert api.link_token_requests[0]["products"] == ["transactions"]
    assert api.link_token_requests[0]["required_if_supported_products"] == ["liabilities"]
    assert result.transaction_days_requested == 730
    assert api.link_token_requests == [
        {
            "client_name": "Plaid MCP",
            "country_codes": ["US"],
            "language": "en",
            "products": ["transactions"],
            "required_if_supported_products": ["liabilities"],
            "redirect_uri": "https://example.test/link/callback",
            "transactions": {"days_requested": 730},
            "user": {"client_user_id": "owner"},
        }
    ]


def test_create_link_token_allows_custom_transaction_history_depth() -> None:
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    result = client.create_link_token(
        products=["transactions"],
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
        products=["investments"], redirect_uri="https://example.test/link/callback", client_user_id="owner"
    )

    assert result.products == ["investments"]
    assert "required_if_supported_products" not in api.link_token_requests[0]
    assert result.transaction_days_requested is None
    assert "transactions" not in api.link_token_requests[0]


def test_link_token_anchors_on_the_broadest_product() -> None:
    """The anchor is the one product that CAN fail the Link, so it must be the one least likely to
    have no eligible account. Liabilities anchors only when it is all that was asked for."""
    api = _FakePlaidApi()
    client = PlaidClient(api=cast(PlaidSdkApiLike, api))

    client.create_link_token(
        products=["liabilities", "investments"], redirect_uri="https://x.test/cb", client_user_id="owner"
    )
    client.create_link_token(products=["liabilities"], redirect_uri="https://x.test/cb", client_user_id="owner")

    assert api.link_token_requests[0]["products"] == ["investments"]
    assert api.link_token_requests[0]["required_if_supported_products"] == ["liabilities"]
    assert api.link_token_requests[1]["products"] == ["liabilities"]


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
