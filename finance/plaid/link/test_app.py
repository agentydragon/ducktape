import base64
import hashlib
import json
import time
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

import jwt
import pytest_bazel
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.institution import Institution
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.institutions_get_by_id_response import InstitutionsGetByIdResponse
from plaid.model.institutions_search_request import InstitutionsSearchRequest
from plaid.model.institutions_search_response import InstitutionsSearchResponse
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.item import Item
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_public_token_exchange_response import ItemPublicTokenExchangeResponse
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.item_remove_response import ItemRemoveResponse
from plaid.model.item_webhook_update_request import ItemWebhookUpdateRequest
from plaid.model.item_webhook_update_response import ItemWebhookUpdateResponse
from plaid.model.jwk_public_key import JWKPublicKey
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_response import LinkTokenCreateResponse
from plaid.model.products import Products
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_sync_response import TransactionsSyncResponse
from plaid.model.webhook_verification_key_get_request import WebhookVerificationKeyGetRequest
from plaid.model.webhook_verification_key_get_response import WebhookVerificationKeyGetResponse

from finance.plaid.db.client import PlaidClient, PlaidSdkApiLike
from finance.plaid.db.config import PlaidWebSettings
from finance.plaid.db.link_store import PlaidLinkStorage, StoredLink, SyncAlreadyRunningError
from finance.plaid.link.app import create_app

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx


class _FakeStorage:
    def __init__(self) -> None:
        self.purged_item_ids: list[str] = []
        self.queued_item_syncs: list[tuple[str, bool]] = []
        self.webhook_deliveries: list[dict[str, str | None]] = []

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

    async def claim_item_sync(self) -> None:
        return None

    async def enqueue_item_sync(self, item_id: str, *, full_sync: bool = False) -> None:
        self.queued_item_syncs.append((item_id, full_sync))

    async def record_plaid_webhook_delivery(self, raw_body: str) -> int:
        self.webhook_deliveries.append({"raw_body": raw_body, "disposition": "received"})
        return len(self.webhook_deliveries)

    async def update_plaid_webhook_delivery(
        self,
        delivery_id: int,
        *,
        webhook_type: str | None,
        webhook_code: str | None,
        item_id: str | None,
        disposition: str,
    ) -> None:
        self.webhook_deliveries[delivery_id - 1].update(
            {"webhook_type": webhook_type, "webhook_code": webhook_code, "item_id": item_id, "disposition": disposition}
        )

    async def finish_item_sync(self, claim: object) -> None:
        raise AssertionError("the fake worker never claims Item syncs")

    async def retry_item_sync(self, claim: object) -> None:
        raise AssertionError("the fake worker never claims Item syncs")


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
        self.webhook_private_key = ec.generate_private_key(ec.SECP256R1())
        self.link_token_requests: list[dict[str, object]] = []
        self.exchanged_public_tokens: list[str] = []
        self.removed_access_tokens: list[str] = []

    def link_token_create(self, request: LinkTokenCreateRequest) -> LinkTokenCreateResponse:
        self.link_token_requests.append(request.to_dict())
        return cast(
            LinkTokenCreateResponse,
            LinkTokenCreateResponse(
                link_token=f"link-token-{len(self.link_token_requests)}",
                expiration=datetime(2026, 6, 1, tzinfo=UTC),
                request_id="req-link-token",
            ),
        )

    def item_public_token_exchange(self, request: ItemPublicTokenExchangeRequest) -> ItemPublicTokenExchangeResponse:
        self.exchanged_public_tokens.append(request.public_token)
        return cast(
            ItemPublicTokenExchangeResponse,
            ItemPublicTokenExchangeResponse(
                access_token="access-sandbox-new", item_id="item-sandbox-new", request_id="req-token-exchange"
            ),
        )

    def item_remove(self, request: ItemRemoveRequest) -> ItemRemoveResponse:
        self.removed_access_tokens.append(request.access_token)
        return cast(ItemRemoveResponse, ItemRemoveResponse(request_id="req-item-remove"))

    def item_webhook_update(self, request: ItemWebhookUpdateRequest) -> ItemWebhookUpdateResponse:
        return cast(
            ItemWebhookUpdateResponse,
            ItemWebhookUpdateResponse(
                item=cast(
                    Item,
                    Item(
                        item_id="item_123",
                        webhook=request.webhook,
                        error=None,
                        available_products=[Products("transactions")],
                        billed_products=[],
                        consent_expiration_time=None,
                        update_type="background",
                        _check_type=False,
                    ),
                ),
                request_id="req-webhook-update",
            ),
        )

    def webhook_verification_key_get(
        self, request: WebhookVerificationKeyGetRequest
    ) -> WebhookVerificationKeyGetResponse:
        assert request.key_id == "test-key"
        numbers = self.webhook_private_key.public_key().public_numbers()

        def encode(value: int) -> str:
            return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode("ascii")

        key = cast(
            JWKPublicKey,
            JWKPublicKey(
                alg="ES256",
                crv="P-256",
                kid="test-key",
                kty="EC",
                use="sig",
                x=encode(numbers.x),
                y=encode(numbers.y),
                created_at=0,
                expired_at=None,
            ),
        )
        return cast(
            WebhookVerificationKeyGetResponse, WebhookVerificationKeyGetResponse(key=key, request_id="req-webhook-key")
        )

    def transactions_sync(self, request: object) -> TransactionsSyncResponse:
        raise AssertionError("unexpected transaction sync in this app test")

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

    def institutions_search(self, request: InstitutionsSearchRequest) -> InstitutionsSearchResponse:
        return cast(
            InstitutionsSearchResponse,
            InstitutionsSearchResponse(institutions=[_institution()], request_id="req-institutions-search"),
        )

    def institutions_get_by_id(self, request: InstitutionsGetByIdRequest) -> InstitutionsGetByIdResponse:
        return cast(
            InstitutionsGetByIdResponse,
            InstitutionsGetByIdResponse(
                institution=_institution(url="https://chase.example"), request_id="req-institutions-get"
            ),
        )


def _institution(*, url: str | None = None) -> Institution:
    return cast(
        Institution,
        Institution(
            institution_id="ins_3",
            name="Chase",
            products=[Products("auth"), Products("transactions"), Products("identity"), Products("liabilities")],
            country_codes=[CountryCode("US")],
            routing_numbers=[],
            oauth=False,
            url=url,
        ),
    )


def _client(
    *,
    storage: _FakeStorage | None = None,
    secrets: _FakeSecrets | None = None,
    api: _FakePlaidApi | None = None,
    signed_in: bool = True,
) -> TestClient:
    settings = PlaidWebSettings(
        plaid_env="sandbox",
        client_id="client-id",
        client_secret="client-secret",
        DATABASE_URL="postgresql://example.invalid/plaid",
        public_base_url="https://plaid-mcp.test",
        webhook_url="https://plaid-mcp.test/webhooks/plaid",
        target_namespace="plaid-mcp",
        oidc_issuer="https://auth.example.test/application/o/plaid-link-oidc/",
        oidc_client_id="plaid-link",
        oidc_client_secret="test-client-secret",
        oidc_session_secret="test-session-secret",
    )
    test_client = TestClient(
        create_app(
            settings,
            storage=cast(PlaidLinkStorage, storage or _FakeStorage()),
            secrets=secrets or _FakeSecrets(),
            client=PlaidClient(api=cast(PlaidSdkApiLike, api or _FakePlaidApi())),
        ),
        base_url=settings.public_base_url,
        headers={"Origin": settings.public_base_url},
    )
    if signed_in:
        session = {
            "user": {
                "issuer": settings.oidc_issuer,
                "subject": "test-subject",
                "username": "agentydragon",
                "expires_at": time.time() + 3600,
            }
        }
        encoded_session = base64.b64encode(json.dumps(session, separators=(",", ":")).encode("utf-8"))
        cookie = TimestampSigner(settings.oidc_session_secret.get_secret_value()).sign(encoded_session).decode("utf-8")
        # The name browsers already hold sessions under: renaming it signs everyone out.
        test_client.cookies.set("__Host-plaid-link-session", cookie, domain="plaid-mcp.test", path="/")
    return test_client


def test_only_health_and_the_signed_webhook_are_open_without_a_session() -> None:
    with _client(signed_in=False) as client:
        health = client.get("/healthz")
        signed_out = client.get("/auth/signed-out")
        webhook = client.post("/webhooks/plaid", content=b"{}")
        api = client.get("/api/links")
        page = client.get("/link", follow_redirects=False)

    assert (health.status_code, signed_out.status_code) == (200, 200)
    assert "signed out of Plaid Link" in signed_out.text
    # The webhook answers for itself, wanting Plaid's signature, rather than the login gate answering for it.
    assert (webhook.status_code, webhook.json()["detail"]) == (401, "missing Plaid-Verification header")
    assert (api.status_code, api.json()) == (401, {"detail": "Not authenticated"})
    assert (page.status_code, page.headers["location"]) == (303, "/auth/login")


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
    assert api.link_token_requests[0]["webhook"] == "https://plaid-mcp.test/webhooks/plaid"


def _signed_webhook(api: _FakePlaidApi, payload: dict[str, object]) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    token = jwt.encode(
        {"iat": int(time.time()), "request_body_sha256": hashlib.sha256(body).hexdigest()},
        api.webhook_private_key,
        algorithm="ES256",
        headers={"kid": "test-key"},
    )
    return body, token


def test_signed_transactions_webhook_is_queued_and_body_tampering_is_rejected() -> None:
    api = _FakePlaidApi()
    storage = _FakeStorage()
    body, token = _signed_webhook(
        api,
        {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "item_123",
            "future_plaid_field": {"kept": True},
        },
    )

    with _client(storage=storage, api=api) as client:
        response = client.post(
            "/webhooks/plaid", content=body, headers={"Plaid-Verification": token, "Content-Type": "application/json"}
        )
        tampered = client.post(
            "/webhooks/plaid",
            content=body + b" ",
            headers={"Plaid-Verification": token, "Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert storage.queued_item_syncs == [("item_123", False)]
    assert storage.webhook_deliveries == [
        {
            "raw_body": body.decode(),
            "disposition": "queued",
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "item_123",
        }
    ]
    assert tampered.status_code == 401


def test_authenticated_unhandled_webhook_is_recorded_and_ignored() -> None:
    api = _FakePlaidApi()
    storage = _FakeStorage()
    body, token = _signed_webhook(
        api,
        {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "INITIAL_UPDATE",
            "item_id": "item_123",
            "new_plaid_field": "preserve this",
        },
    )

    with _client(storage=storage, api=api) as client:
        response = client.post(
            "/webhooks/plaid", content=body, headers={"Plaid-Verification": token, "Content-Type": "application/json"}
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert storage.queued_item_syncs == []
    assert storage.webhook_deliveries == [
        {
            "raw_body": body.decode(),
            "disposition": "ignored",
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "INITIAL_UPDATE",
            "item_id": "item_123",
        }
    ]


def test_authenticated_unrecognized_envelope_body_is_recorded() -> None:
    api = _FakePlaidApi()
    storage = _FakeStorage()
    body, token = _signed_webhook(api, {"future_envelope": {"value": 42}})

    with _client(storage=storage, api=api) as client:
        response = client.post(
            "/webhooks/plaid", content=body, headers={"Plaid-Verification": token, "Content-Type": "application/json"}
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert storage.webhook_deliveries == [
        {
            "raw_body": body.decode(),
            "disposition": "ignored",
            "webhook_type": None,
            "webhook_code": None,
            "item_id": None,
        }
    ]


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
