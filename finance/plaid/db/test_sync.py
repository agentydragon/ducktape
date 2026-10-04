import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from plaid.exceptions import ApiException as PlaidApiException
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_webhook_update_request import ItemWebhookUpdateRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from finance.plaid.db.link_store import ApiEvent, PlaidLinkStorage, StoredLink
from finance.plaid.db.sync import redact_payload, sync_all, sync_link, sync_transactions_only


def test_redact_payload_returns_json_serializable_dates() -> None:
    payload = redact_payload(
        {
            "access_token": "secret",
            "start_date": date(2026, 5, 1),
            "nested": [{"captured_at": datetime(2026, 5, 31, 2, 7, tzinfo=UTC)}],
        }
    )

    assert payload == {
        "access_token": "<redacted>",
        "start_date": "2026-05-01",
        "nested": [{"captured_at": "2026-05-31T02:07:00+00:00"}],
    }


def _stored_link(item_id: str, products: list[str], cursor: str | None = None) -> StoredLink:
    return StoredLink(
        item_id=item_id,
        label=None,
        institution_id="ins_1",
        institution_name="Testbank",
        products_requested=products,
        transaction_days_requested=None,
        products_authorized=products,
        products_billed=products,
        status="active",
        access_token_secret=f"secret-{item_id}",
        last_synced_at=None,
        transactions_cursor=cursor,
    )


def _transaction(transaction_id: str) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "account_id": "account-1",
        "date": "2026-09-28",
        "amount": 1.0,
        "name": transaction_id,
        "pending": False,
    }


def _sync_response(
    *,
    added: list[dict[str, Any]] | None = None,
    modified: list[dict[str, Any]] | None = None,
    removed: list[dict[str, Any]] | None = None,
    has_more: bool = False,
    next_cursor: str = "cursor-next",
) -> dict[str, Any]:
    return {
        "accounts": [],
        "added": added or [],
        "modified": modified or [],
        "removed": removed or [],
        "has_more": has_more,
        "next_cursor": next_cursor,
        "transactions_update_status": "HISTORICAL_UPDATE",
    }


def _no_investment_accounts_error() -> PlaidApiException:
    exc = PlaidApiException(status=400, reason="Bad Request")
    exc.body = json.dumps({"error_type": "ITEM_ERROR", "error_code": "NO_INVESTMENT_ACCOUNTS"})
    return exc


def _no_liability_accounts_error() -> PlaidApiException:
    exc = PlaidApiException(status=400, reason="Bad Request")
    exc.body = json.dumps({"error_type": "ITEM_ERROR", "error_code": "NO_LIABILITY_ACCOUNTS"})
    return exc


class _FakeApiClient:
    def sanitize_for_serialization(self, obj: object) -> object:
        return obj


class _FakeApi:
    """PlaidApiLike fake; raises `errors[endpoint, access_token]` from that one endpoint."""

    def __init__(self, errors: dict[tuple[str, str], Exception] | None = None) -> None:
        self.api_client = _FakeApiClient()
        self._errors = errors or {}
        self.liabilities_calls = 0
        self.investment_transaction_calls = 0
        self.sync_responses: list[dict[str, Any] | Exception] = []
        self.sync_requests: list[TransactionsSyncRequest] = []
        self.item_webhooks: list[str | None] = []
        self.item_webhook: str | None = None

    def _maybe_raise(self, endpoint: str, access_token: str) -> None:
        if (exc := self._errors.get((endpoint, access_token))) is not None:
            raise exc

    def item_get(self, request: ItemGetRequest, /) -> object:
        self._maybe_raise("item/get", request.access_token)
        return {"item": {"webhook": self.item_webhook}, "request_id": "req-item"}

    def item_webhook_update(self, request: ItemWebhookUpdateRequest, /) -> object:
        self.item_webhooks.append(request.webhook)
        return {"request_id": "req-webhook"}

    def accounts_get(self, request: AccountsGetRequest, /) -> object:
        self._maybe_raise("accounts/get", request.access_token)
        return {"accounts": [], "request_id": "req-accounts"}

    def transactions_sync(self, request: TransactionsSyncRequest, /) -> object:
        self.sync_requests.append(request)
        self._maybe_raise("transactions/sync", request.access_token)
        if self.sync_responses:
            response = self.sync_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return _sync_response()

    def investments_holdings_get(self, request: InvestmentsHoldingsGetRequest, /) -> object:
        self._maybe_raise("investments/holdings/get", request.access_token)
        return {"securities": [], "holdings": [], "request_id": "req-hold"}

    def investments_transactions_get(self, request: InvestmentsTransactionsGetRequest, /) -> object:
        self.investment_transaction_calls += 1
        self._maybe_raise("investments/transactions/get", request.access_token)
        return {"total_investment_transactions": 0, "investment_transactions": [], "request_id": "req-itxn"}

    def liabilities_get(self, request: LiabilitiesGetRequest, /) -> object:
        self.liabilities_calls += 1
        self._maybe_raise("liabilities/get", request.access_token)
        return {"liabilities": {}, "request_id": "req-liab"}


@dataclass
class _FakeStorage:
    """PlaidLinkStorage fake recording the calls the sync makes."""

    links: list[StoredLink] = field(default_factory=list)
    begun_items: list[str | None] = field(default_factory=list)
    finished: list[tuple[UUID, str, str | None]] = field(default_factory=list)
    liability_snapshots: list[str] = field(default_factory=list)
    account_refreshes: list[str] = field(default_factory=list)
    transaction_deltas: list[dict[str, Any]] = field(default_factory=list)

    async def list_active_links(self) -> list[StoredLink]:
        return self.links

    async def begin_sync_run(self, *, trigger: str, item_id: str | None, configured_windows: dict[str, Any]) -> UUID:
        self.begun_items.append(item_id)
        return uuid4()

    async def finish_sync_run(self, run_id: UUID, *, status: str, error_summary: str | None = None) -> None:
        self.finished.append((run_id, status, error_summary))

    async def record_api_event(self, event: ApiEvent) -> None:
        pass

    async def upsert_link(self, **kwargs: Any) -> None:
        pass

    async def apply_accounts(self, *, item_id: str, accounts: list[dict[str, Any]], captured_at: datetime) -> None:
        self.account_refreshes.append(item_id)

    async def apply_transaction_delta(self, **kwargs: Any) -> None:
        self.transaction_deltas.append(kwargs)

    async def apply_holdings(self, **kwargs: Any) -> None:
        pass

    async def upsert_investment_transactions(self, **kwargs: Any) -> None:
        pass

    async def append_liability_snapshots(
        self, *, item_id: str, liabilities: dict[str, Any], captured_at: datetime
    ) -> None:
        self.liability_snapshots.append(item_id)


class _FakeSecrets:
    async def read_access_token(self, secret_name: str) -> str:
        return f"token-for-{secret_name}"

    async def write_access_token(self, secret_name: str, access_token: str) -> None:
        raise NotImplementedError

    async def delete_access_token(self, secret_name: str) -> None:
        raise NotImplementedError


async def test_investment_transactions_follow_the_requested_products() -> None:
    """This used to be gated on the link's profile name, which drifted from the products actually
    requested; the profile is gone and products_requested is the only signal."""
    link = _stored_link("item-merrill", ["investments"])
    storage = _FakeStorage(links=[link])
    api = _FakeApi()

    await sync_link(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link, trigger="test")

    assert api.investment_transaction_calls == 1


async def test_sync_link_tolerates_no_investment_accounts() -> None:
    """Products are requested per institution but authorized per Item: Chase-the-institution offers
    investments while a Chase Item holding one credit card has none, and Plaid 400s rather than
    returning an empty set. That took down the whole Chase sync, transactions included."""
    link = _stored_link("item-chase", ["transactions", "investments"])
    storage = _FakeStorage(links=[link])
    api = _FakeApi(
        errors={("investments/holdings/get", "token-for-secret-item-chase"): _no_investment_accounts_error()}
    )

    await sync_link(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link, trigger="test")

    assert [(status, err) for _, status, err in storage.finished] == [("succeeded", None)]
    # The investment-transaction call belongs to the same block and must not fire either.
    assert api.investment_transaction_calls == 0


async def test_sync_link_tolerates_no_liability_accounts() -> None:
    link = _stored_link("item-merrill", ["transactions", "liabilities"])
    storage = _FakeStorage(links=[link])
    api = _FakeApi(errors={("liabilities/get", "token-for-secret-item-merrill"): _no_liability_accounts_error()})

    await sync_link(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link, trigger="test")

    assert api.liabilities_calls == 1
    assert storage.liability_snapshots == []
    assert [(status, err) for _, status, err in storage.finished] == [("succeeded", None)]


async def test_sync_link_updates_existing_item_webhook() -> None:
    link = _stored_link("item-chase", ["transactions"])
    storage = _FakeStorage(links=[link])
    api = _FakeApi()

    await sync_link(
        api=api,
        storage=cast(PlaidLinkStorage, storage),
        secrets=_FakeSecrets(),
        link=link,
        trigger="test",
        webhook_url="https://plaid-mcp.test/webhooks/plaid",
    )

    assert api.item_webhooks == ["https://plaid-mcp.test/webhooks/plaid"]


async def test_sync_link_reraises_other_liability_errors() -> None:
    exc = PlaidApiException(status=400, reason="Bad Request")
    exc.body = json.dumps({"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED"})
    link = _stored_link("item-merrill", ["liabilities"])
    storage = _FakeStorage(links=[link])
    api = _FakeApi(errors={("liabilities/get", "token-for-secret-item-merrill"): exc})

    with pytest.raises(PlaidApiException):
        await sync_link(
            api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link, trigger="test"
        )

    assert [(status,) for _, status, _ in storage.finished] == [("failed",)]


async def test_sync_all_keeps_going_past_a_failing_link() -> None:
    failing = _stored_link("item-bad", ["transactions"])
    healthy = _stored_link("item-good", ["transactions"])
    storage = _FakeStorage(links=[failing, healthy])
    api = _FakeApi(errors={("item/get", "token-for-secret-item-bad"): RuntimeError("bank exploded")})

    with pytest.raises(ExceptionGroup):
        await sync_all(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), trigger="test")

    assert storage.begun_items == ["item-bad", "item-good"]
    assert [(status, err is None) for _, status, err in storage.finished] == [("failed", False), ("succeeded", True)]


async def test_transaction_sync_accumulates_pages_before_advancing_cursor() -> None:
    link = _stored_link("item-chase", ["transactions"], cursor="cursor-old")
    storage = _FakeStorage(links=[link])
    api = _FakeApi()
    api.sync_responses = [
        _sync_response(added=[_transaction("one")], has_more=True, next_cursor="page-1"),
        _sync_response(modified=[_transaction("two")], next_cursor="cursor-new"),
    ]

    await sync_link(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link, trigger="test")

    assert [request.cursor for request in api.sync_requests] == ["cursor-old", "page-1"]
    assert [txn["transaction_id"] for txn in storage.transaction_deltas[0]["added"]] == ["one"]
    assert [txn["transaction_id"] for txn in storage.transaction_deltas[0]["modified"]] == ["two"]
    assert storage.transaction_deltas[0]["next_cursor"] == "cursor-new"


async def test_webhook_sync_refreshes_accounts_before_transaction_delta() -> None:
    link = _stored_link("item-chase", ["transactions"], cursor="cursor-old")
    storage = _FakeStorage(links=[link])
    api = _FakeApi()

    await sync_transactions_only(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link)

    assert storage.account_refreshes == ["item-chase"]
    assert api.sync_requests[0].cursor == "cursor-old"


async def test_transaction_sync_restarts_mutated_pagination_from_saved_cursor() -> None:
    mutation = PlaidApiException(status=400, reason="Bad Request")
    mutation.body = json.dumps(
        {"error_type": "TRANSACTIONS_ERROR", "error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"}
    )
    link = _stored_link("item-chase", ["transactions"], cursor="cursor-old")
    storage = _FakeStorage(links=[link])
    api = _FakeApi()
    first_page = _sync_response(added=[_transaction("stable")], has_more=True, next_cursor="page-1")
    last_page = _sync_response(next_cursor="cursor-new")
    api.sync_responses = [first_page, mutation, first_page, last_page]

    await sync_link(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link, trigger="test")

    assert [request.cursor for request in api.sync_requests] == ["cursor-old", "page-1", "cursor-old", "page-1"]
    assert len(storage.transaction_deltas) == 1
    assert [txn["transaction_id"] for txn in storage.transaction_deltas[0]["added"]] == ["stable"]
    assert storage.transaction_deltas[0]["next_cursor"] == "cursor-new"


if __name__ == "__main__":
    pytest_bazel.main()
