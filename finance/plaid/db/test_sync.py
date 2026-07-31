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
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_get_request import TransactionsGetRequest

from finance.plaid.db.link_profiles import LinkProfile
from finance.plaid.db.link_store import ApiEvent, PlaidLinkStorage, StoredLink
from finance.plaid.db.sync import redact_payload, sync_all, sync_link


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
    json.dumps(payload)


def _stored_link(item_id: str, products: list[str]) -> StoredLink:
    return StoredLink(
        item_id=item_id,
        label=None,
        institution_id="ins_1",
        institution_name="Testbank",
        link_profile=LinkProfile.CREDIT_CARD_DETAIL,
        products_requested=products,
        transaction_days_requested=None,
        products_authorized=products,
        products_billed=products,
        status="active",
        access_token_secret=f"secret-{item_id}",
        last_synced_at=None,
    )


def _no_liability_accounts_error() -> PlaidApiException:
    exc = PlaidApiException(status=400, reason="Bad Request")
    exc.body = json.dumps({"error_type": "ITEM_ERROR", "error_code": "NO_LIABILITY_ACCOUNTS"})
    return exc


class _FakeApiClient:
    def sanitize_for_serialization(self, obj: object) -> object:
        return obj


class _FakeApi:
    """PlaidApiLike fake; raises `errors[access_token]` from every endpoint for that item."""

    def __init__(self, errors: dict[str, Exception] | None = None) -> None:
        self.api_client = _FakeApiClient()
        self._errors = errors or {}
        self.liabilities_calls = 0

    def _maybe_raise(self, access_token: str) -> None:
        if access_token in self._errors:
            raise self._errors[access_token]

    def item_get(self, request: ItemGetRequest, /) -> object:
        self._maybe_raise(request.access_token)
        return {"item": {}, "request_id": "req-item"}

    def accounts_get(self, request: AccountsGetRequest, /) -> object:
        self._maybe_raise(request.access_token)
        return {"accounts": [], "request_id": "req-accounts"}

    def transactions_get(self, request: TransactionsGetRequest, /) -> object:
        self._maybe_raise(request.access_token)
        return {"total_transactions": 0, "transactions": [], "request_id": "req-txn"}

    def investments_holdings_get(self, request: InvestmentsHoldingsGetRequest, /) -> object:
        self._maybe_raise(request.access_token)
        return {"securities": [], "holdings": [], "request_id": "req-hold"}

    def investments_transactions_get(self, request: InvestmentsTransactionsGetRequest, /) -> object:
        self._maybe_raise(request.access_token)
        return {"total_investment_transactions": 0, "investment_transactions": [], "request_id": "req-itxn"}

    def liabilities_get(self, request: LiabilitiesGetRequest, /) -> object:
        self.liabilities_calls += 1
        self._maybe_raise(request.access_token)
        return {"liabilities": {}, "request_id": "req-liab"}


@dataclass
class _FakeStorage:
    """PlaidLinkStorage fake recording the calls the sync makes."""

    links: list[StoredLink] = field(default_factory=list)
    begun_items: list[str | None] = field(default_factory=list)
    finished: list[tuple[UUID, str, str | None]] = field(default_factory=list)
    liability_snapshots: list[str] = field(default_factory=list)

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
        pass

    async def reconcile_transactions(self, **kwargs: Any) -> None:
        pass

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


async def test_sync_link_tolerates_no_liability_accounts() -> None:
    link = _stored_link("item-merrill", ["transactions", "liabilities"])
    storage = _FakeStorage(links=[link])
    api = _FakeApi(errors={"token-for-secret-item-merrill": _no_liability_accounts_error()})

    await sync_link(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link, trigger="test")

    assert api.liabilities_calls == 1
    assert storage.liability_snapshots == []
    assert [(status, err) for _, status, err in storage.finished] == [("succeeded", None)]


async def test_sync_link_reraises_other_liability_errors() -> None:
    exc = PlaidApiException(status=400, reason="Bad Request")
    exc.body = json.dumps({"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED"})
    link = _stored_link("item-merrill", ["liabilities"])
    storage = _FakeStorage(links=[link])
    api = _FakeApi(errors={"token-for-secret-item-merrill": exc})

    with pytest.raises(PlaidApiException):
        await sync_link(
            api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), link=link, trigger="test"
        )

    assert [(status,) for _, status, _ in storage.finished] == [("failed",)]


async def test_sync_all_keeps_going_past_a_failing_link() -> None:
    failing = _stored_link("item-bad", ["transactions"])
    healthy = _stored_link("item-good", ["transactions"])
    storage = _FakeStorage(links=[failing, healthy])
    api = _FakeApi(errors={"token-for-secret-item-bad": RuntimeError("bank exploded")})

    with pytest.raises(ExceptionGroup):
        await sync_all(api=api, storage=cast(PlaidLinkStorage, storage), secrets=_FakeSecrets(), trigger="test")

    assert storage.begun_items == ["item-bad", "item-good"]
    assert [(status, err is None) for _, status, err in storage.finished] == [("failed", False), ("succeeded", True)]


if __name__ == "__main__":
    pytest_bazel.main()
