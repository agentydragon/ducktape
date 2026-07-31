"""Synchronous v0 full-refresh sync from Plaid into Postgres."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID

from plaid.exceptions import ApiException as PlaidApiException
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.investments_transactions_get_request_options import InvestmentsTransactionsGetRequestOptions
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

from finance.plaid.db.link_profiles import Product, syncs_investment_transactions
from finance.plaid.db.link_store import ApiEvent, PlaidLinkStorage, StoredLink
from finance.plaid.db.secret_store import SecretStore

logger = logging.getLogger(__name__)


class PlaidRequestLike(Protocol):
    """Common shape of plaid-python generated request objects."""

    def to_dict(self) -> dict[str, Any]: ...


class PlaidApiClientLike(Protocol):
    """The generated SDK's nested ApiClient serializer."""

    def sanitize_for_serialization(self, obj: object) -> object: ...


class PlaidApiLike(Protocol):
    """Minimal Plaid SDK client surface used by the synchronous sync job."""

    @property
    def api_client(self) -> PlaidApiClientLike: ...

    def item_get(self, request: ItemGetRequest, /) -> object: ...
    def accounts_get(self, request: AccountsGetRequest, /) -> object: ...
    def transactions_get(self, request: TransactionsGetRequest, /) -> object: ...
    def investments_holdings_get(self, request: InvestmentsHoldingsGetRequest, /) -> object: ...
    def investments_transactions_get(self, request: InvestmentsTransactionsGetRequest, /) -> object: ...
    def liabilities_get(self, request: LiabilitiesGetRequest, /) -> object: ...


@dataclass(frozen=True)
class SyncWindows:
    transaction_days: int = 730
    investment_transaction_days: int = 730

    def as_dict(self) -> dict[str, int]:
        return {
            "transaction_days": self.transaction_days,
            "investment_transaction_days": self.investment_transaction_days,
        }


def redact_payload(value: Any) -> Any:
    """Return JSON-ish value with Plaid secrets removed."""
    sensitive = {"access_token", "public_token", "client_id", "secret", "client_secret", "authorization"}
    if isinstance(value, dict):
        return {k: ("<redacted>" if k.lower() in sensitive else redact_payload(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_payload(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


async def sync_all(
    *,
    api: PlaidApiLike,
    storage: PlaidLinkStorage,
    secrets: SecretStore,
    trigger: str = "cron",
    windows: SyncWindows | None = None,
) -> list[UUID]:
    """Sync every active link; one link's failure must not starve the links after it."""
    sync_windows = windows or SyncWindows()
    run_ids: list[UUID] = []
    failures: list[Exception] = []
    for link in await storage.list_active_links():
        try:
            run_ids.append(
                await sync_link(
                    api=api, storage=storage, secrets=secrets, link=link, trigger=trigger, windows=sync_windows
                )
            )
        except Exception as exc:
            logger.exception("sync failed for item %s (%s)", link.item_id, link.institution_name)
            failures.append(exc)
    if failures:
        raise ExceptionGroup(f"{len(failures)} link sync(s) failed", failures)
    return run_ids


async def sync_link(
    *,
    api: PlaidApiLike,
    storage: PlaidLinkStorage,
    secrets: SecretStore,
    link: StoredLink,
    trigger: str,
    windows: SyncWindows | None = None,
) -> UUID:
    sync_windows = windows or SyncWindows()
    run_id = await storage.begin_sync_run(
        trigger=trigger, item_id=link.item_id, configured_windows=sync_windows.as_dict()
    )
    try:
        await _sync_link_inner(
            api=api, storage=storage, secrets=secrets, link=link, run_id=run_id, windows=sync_windows
        )
    except Exception as exc:
        await storage.finish_sync_run(run_id, status="failed", error_summary=f"{type(exc).__name__}: {exc}")
        raise
    await storage.finish_sync_run(run_id, status="succeeded")
    return run_id


async def _sync_link_inner(
    *,
    api: PlaidApiLike,
    storage: PlaidLinkStorage,
    secrets: SecretStore,
    link: StoredLink,
    run_id: UUID,
    windows: SyncWindows,
) -> None:
    access_token = await secrets.read_access_token(link.access_token_secret)
    captured_at = datetime.now(UTC)

    item = await _call(
        api, storage, run_id, "item/get", api.item_get, ItemGetRequest(access_token=access_token), link.item_id
    )
    item_payload = item.get("item", {})
    await storage.upsert_link(
        item_id=link.item_id,
        access_token_secret=link.access_token_secret,
        link_profile=link.link_profile,
        products_requested=link.products_requested,
        transaction_days_requested=link.transaction_days_requested,
        products_authorized=item_payload.get("products") or link.products_authorized,
        products_billed=item_payload.get("billed_products") or link.products_billed,
        institution_id=item_payload.get("institution_id") or link.institution_id,
        institution_name=item_payload.get("institution_name") or link.institution_name,
        label=link.label,
        status="active",
    )

    accounts_payload = await _call(
        api,
        storage,
        run_id,
        "accounts/get",
        api.accounts_get,
        AccountsGetRequest(access_token=access_token),
        link.item_id,
    )
    await storage.apply_accounts(
        item_id=link.item_id, accounts=accounts_payload.get("accounts") or [], captured_at=captured_at
    )

    if Product.TRANSACTIONS.value in link.products_requested:
        end = captured_at.date()
        start = end - timedelta(days=windows.transaction_days)
        transactions = await _fetch_transactions(api, storage, run_id, access_token, link.item_id, start, end)
        await storage.reconcile_transactions(
            item_id=link.item_id, start_date=start, end_date=end, transactions=transactions, captured_at=captured_at
        )

    if Product.INVESTMENTS.value in link.products_requested:
        holdings = await _call(
            api,
            storage,
            run_id,
            "investments/holdings/get",
            api.investments_holdings_get,
            InvestmentsHoldingsGetRequest(access_token=access_token),
            link.item_id,
        )
        await storage.apply_holdings(
            item_id=link.item_id,
            securities=holdings.get("securities") or [],
            holdings=holdings.get("holdings") or [],
            captured_at=captured_at,
        )
        if syncs_investment_transactions(link.link_profile):
            end = captured_at.date()
            start = end - timedelta(days=windows.investment_transaction_days)
            txns = await _fetch_investment_transactions(api, storage, run_id, access_token, link.item_id, start, end)
            await storage.upsert_investment_transactions(
                item_id=link.item_id, transactions=txns, captured_at=captured_at
            )

    if Product.LIABILITIES.value in link.products_requested:
        try:
            liabilities = await _call(
                api,
                storage,
                run_id,
                "liabilities/get",
                api.liabilities_get,
                LiabilitiesGetRequest(access_token=access_token),
                link.item_id,
            )
        except PlaidApiException as exc:
            if _plaid_error_code(exc) != "NO_LIABILITY_ACCOUNTS":
                raise
            # The item currently has no liability accounts (e.g. its last card or loan was
            # closed); Plaid 400s instead of returning an empty set. Nothing to snapshot.
            logger.warning("liabilities/get: item %s has no liability accounts; skipping", link.item_id)
        else:
            await storage.append_liability_snapshots(
                item_id=link.item_id, liabilities=liabilities.get("liabilities") or {}, captured_at=captured_at
            )


async def _fetch_transactions(
    api: PlaidApiLike, storage: PlaidLinkStorage, run_id: UUID, access_token: str, item_id: str, start: date, end: date
) -> list[dict[str, Any]]:
    offset = 0
    count = 500
    out: list[dict[str, Any]] = []
    total = None
    while total is None or offset < total:
        payload = await _call(
            api,
            storage,
            run_id,
            "transactions/get",
            api.transactions_get,
            TransactionsGetRequest(
                access_token=access_token,
                start_date=start,
                end_date=end,
                options=TransactionsGetRequestOptions(offset=offset, count=count),
            ),
            item_id,
        )
        total = payload["total_transactions"]
        page = payload.get("transactions") or []
        out.extend(page)
        offset += len(page)
        if not page:
            break
    return out


async def _fetch_investment_transactions(
    api: PlaidApiLike, storage: PlaidLinkStorage, run_id: UUID, access_token: str, item_id: str, start: date, end: date
) -> list[dict[str, Any]]:
    offset = 0
    count = 500
    out: list[dict[str, Any]] = []
    total = None
    while total is None or offset < total:
        payload = await _call(
            api,
            storage,
            run_id,
            "investments/transactions/get",
            api.investments_transactions_get,
            InvestmentsTransactionsGetRequest(
                access_token=access_token,
                start_date=start,
                end_date=end,
                options=InvestmentsTransactionsGetRequestOptions(offset=offset, count=count),
            ),
            item_id,
        )
        total = payload["total_investment_transactions"]
        page = payload.get("investment_transactions") or []
        out.extend(page)
        offset += len(page)
        if not page:
            break
    return out


async def _call[PlaidRequestT: PlaidRequestLike](
    api: PlaidApiLike,
    storage: PlaidLinkStorage,
    run_id: UUID,
    endpoint: str,
    call: Callable[[PlaidRequestT], object],
    request: PlaidRequestT,
    item_id: str,
) -> dict[str, Any]:
    started = time.monotonic()
    request_json = _request_json(request)
    try:
        response = await asyncio.to_thread(call, request)
        response_json = cast(dict[str, Any], api.api_client.sanitize_for_serialization(response))
    except Exception as exc:
        await storage.record_api_event(
            ApiEvent(
                sync_run_id=run_id,
                endpoint=endpoint,
                item_id=item_id,
                status="error",
                duration_ms=int((time.monotonic() - started) * 1000),
                error_type=type(exc).__name__,
                error_code=_plaid_error_code(exc),
                request_json=redact_payload(request_json),
            )
        )
        raise
    await storage.record_api_event(
        ApiEvent(
            sync_run_id=run_id,
            endpoint=endpoint,
            item_id=item_id,
            request_id=_extract_request_id(response_json),
            status="ok",
            duration_ms=int((time.monotonic() - started) * 1000),
            request_json=redact_payload(request_json),
            response_json=redact_payload(response_json),
        )
    )
    return response_json


def _request_json(request: PlaidRequestLike) -> dict[str, Any]:
    return cast(dict[str, Any], request.to_dict())


def _plaid_error_code(exc: Exception) -> str | None:
    """Plaid's machine-readable error_code (e.g. NO_LIABILITY_ACCOUNTS), or the HTTP status."""
    if not isinstance(exc, PlaidApiException):
        return None
    try:
        parsed = json.loads(exc.body)
    except (TypeError, ValueError):
        # Body absent or not JSON — not a structured Plaid error; fall through to the status.
        parsed = None
    if isinstance(parsed, dict) and isinstance(code := parsed.get("error_code"), str):
        return code
    return str(exc.status) if exc.status is not None else None


def _extract_request_id(response: dict[str, Any]) -> str | None:
    return response.get("request_id") or response.get("requestId")
