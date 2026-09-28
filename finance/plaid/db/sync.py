"""Sync Plaid Items and their product snapshots into Postgres."""

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
from plaid.model.item_webhook_update_request import ItemWebhookUpdateRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from finance.plaid.db.link_store import ApiEvent, PlaidLinkStorage, StoredLink, SyncAlreadyRunningError
from finance.plaid.db.models import (
    AccountsGetResponse,
    InvestmentsHoldingsGetResponse,
    InvestmentsTransactionsGetResponse,
    ItemGetResponse,
    ItemWebhookUpdateResponse,
    LiabilitiesGetResponse,
    PlaidApiResponse,
    PlaidInvestmentTransaction,
    PlaidRemovedTransaction,
    PlaidTransaction,
    TransactionsSyncResponse,
)
from finance.plaid.db.products import Product
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
    def item_webhook_update(self, request: ItemWebhookUpdateRequest, /) -> object: ...
    def accounts_get(self, request: AccountsGetRequest, /) -> object: ...
    def transactions_sync(self, request: TransactionsSyncRequest, /) -> object: ...
    def investments_holdings_get(self, request: InvestmentsHoldingsGetRequest, /) -> object: ...
    def investments_transactions_get(self, request: InvestmentsTransactionsGetRequest, /) -> object: ...
    def liabilities_get(self, request: LiabilitiesGetRequest, /) -> object: ...


@dataclass(frozen=True)
class SyncWindows:
    investment_transaction_days: int = 730

    def as_dict(self) -> dict[str, int | str]:
        return {"transactions": "cursor", "investment_transaction_days": self.investment_transaction_days}


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
    webhook_url: str | None = None,
) -> list[UUID]:
    """Sync every active link; one link's failure must not starve the links after it."""
    sync_windows = windows or SyncWindows()
    run_ids: list[UUID] = []
    failures: list[Exception] = []
    for link in await storage.list_active_links():
        try:
            run_ids.append(
                await sync_link(
                    api=api,
                    storage=storage,
                    secrets=secrets,
                    link=link,
                    trigger=trigger,
                    windows=sync_windows,
                    webhook_url=webhook_url,
                )
            )
        except SyncAlreadyRunningError:
            logger.info("skipping already-running sync for item %s", link.item_id)
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
    webhook_url: str | None = None,
) -> UUID:
    sync_windows = windows or SyncWindows()
    run_id = await storage.begin_sync_run(
        trigger=trigger, item_id=link.item_id, configured_windows=sync_windows.as_dict()
    )
    try:
        await _sync_link_inner(
            api=api,
            storage=storage,
            secrets=secrets,
            link=link,
            run_id=run_id,
            windows=sync_windows,
            webhook_url=webhook_url,
        )
    except asyncio.CancelledError:
        await storage.finish_sync_run(run_id, status="failed", error_summary="sync task was cancelled")
        raise
    except Exception as exc:
        await storage.finish_sync_run(run_id, status="failed", error_summary=f"{type(exc).__name__}: {exc}")
        raise
    await storage.finish_sync_run(run_id, status="succeeded")
    return run_id


async def sync_transactions_only(
    *, api: PlaidApiLike, storage: PlaidLinkStorage, secrets: SecretStore, link: StoredLink, trigger: str = "webhook"
) -> UUID:
    run_id = await storage.begin_sync_run(
        trigger=trigger, item_id=link.item_id, configured_windows={"transactions": "cursor"}
    )
    try:
        access_token = await secrets.read_access_token(link.access_token_secret)
        captured_at = datetime.now(UTC)
        accounts_payload = await _call(
            api,
            storage,
            run_id,
            "accounts/get",
            api.accounts_get,
            AccountsGetRequest(access_token=access_token),
            link.item_id,
            response_model=AccountsGetResponse,
        )
        await storage.apply_accounts(
            item_id=link.item_id, accounts=accounts_payload.accounts or [], captured_at=captured_at
        )
        await _sync_transactions_inner(
            api=api,
            storage=storage,
            run_id=run_id,
            access_token=access_token,
            item_id=link.item_id,
            cursor=link.transactions_cursor,
            captured_at=captured_at,
        )
    except asyncio.CancelledError:
        await storage.finish_sync_run(run_id, status="failed", error_summary="sync task was cancelled")
        raise
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
    webhook_url: str | None,
) -> None:
    access_token = await secrets.read_access_token(link.access_token_secret)
    captured_at = datetime.now(UTC)

    item = await _call(
        api,
        storage,
        run_id,
        "item/get",
        api.item_get,
        ItemGetRequest(access_token=access_token),
        link.item_id,
        response_model=ItemGetResponse,
    )
    item_payload = item.item
    await storage.upsert_link(
        item_id=link.item_id,
        access_token_secret=link.access_token_secret,
        products_requested=link.products_requested,
        transaction_days_requested=link.transaction_days_requested,
        products_authorized=item_payload.products or link.products_authorized,
        products_billed=item_payload.billed_products or link.products_billed,
        institution_id=item_payload.institution_id or link.institution_id,
        institution_name=item_payload.institution_name or link.institution_name,
        label=link.label,
        status="active",
    )
    if (
        Product.TRANSACTIONS.value in link.products_requested
        and webhook_url is not None
        and item_payload.webhook != webhook_url
    ):
        await _call(
            api,
            storage,
            run_id,
            "item/webhook/update",
            api.item_webhook_update,
            ItemWebhookUpdateRequest(access_token=access_token, webhook=webhook_url),
            link.item_id,
            response_model=ItemWebhookUpdateResponse,
        )

    accounts_payload = await _call(
        api,
        storage,
        run_id,
        "accounts/get",
        api.accounts_get,
        AccountsGetRequest(access_token=access_token),
        link.item_id,
        response_model=AccountsGetResponse,
    )
    await storage.apply_accounts(
        item_id=link.item_id, accounts=accounts_payload.accounts or [], captured_at=captured_at
    )

    if Product.TRANSACTIONS.value in link.products_requested:
        await _sync_transactions_inner(
            api=api,
            storage=storage,
            run_id=run_id,
            access_token=access_token,
            item_id=link.item_id,
            cursor=link.transactions_cursor,
            captured_at=captured_at,
        )

    if Product.INVESTMENTS.value in link.products_requested:
        try:
            holdings = await _call(
                api,
                storage,
                run_id,
                "investments/holdings/get",
                api.investments_holdings_get,
                InvestmentsHoldingsGetRequest(access_token=access_token),
                link.item_id,
                response_model=InvestmentsHoldingsGetResponse,
            )
        except PlaidApiException as exc:
            if _plaid_error_code(exc) != "NO_INVESTMENT_ACCOUNTS":
                raise
            # Products are requested per *institution*, but Plaid authorizes them per *Item*: the
            # Chase institution offers investments while a Chase Item holding one credit card has no
            # investment accounts, and Plaid 400s rather than returning an empty set. Same shape as
            # NO_LIABILITY_ACCOUNTS below — a mismatch to skip, not a sync to fail.
            logger.warning("investments/holdings/get: item %s has no investment accounts; skipping", link.item_id)
        else:
            await storage.apply_holdings(
                item_id=link.item_id,
                securities=holdings.securities or [],
                holdings=holdings.holdings or [],
                captured_at=captured_at,
            )
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
                response_model=LiabilitiesGetResponse,
            )
        except PlaidApiException as exc:
            if _plaid_error_code(exc) != "NO_LIABILITY_ACCOUNTS":
                raise
            # The item currently has no liability accounts (e.g. its last card or loan was
            # closed); Plaid 400s instead of returning an empty set. Nothing to snapshot.
            logger.warning("liabilities/get: item %s has no liability accounts; skipping", link.item_id)
        else:
            await storage.append_liability_snapshots(
                item_id=link.item_id, liabilities=liabilities.liabilities or {}, captured_at=captured_at
            )


async def _sync_transactions_inner(
    *,
    api: PlaidApiLike,
    storage: PlaidLinkStorage,
    run_id: UUID,
    access_token: str,
    item_id: str,
    cursor: str | None,
    captured_at: datetime | None = None,
) -> None:
    original_cursor = cursor
    for attempt in range(3):
        page_cursor = original_cursor
        added: list[PlaidTransaction] = []
        modified: list[PlaidTransaction] = []
        removed: list[PlaidRemovedTransaction] = []
        try:
            while True:
                request_args: dict[str, object] = {"access_token": access_token}
                if page_cursor is not None:
                    request_args["cursor"] = page_cursor
                payload = await _call(
                    api,
                    storage,
                    run_id,
                    "transactions/sync",
                    api.transactions_sync,
                    TransactionsSyncRequest(**request_args),
                    item_id,
                    response_model=TransactionsSyncResponse,
                )
                added.extend(payload.added)
                modified.extend(payload.modified)
                removed.extend(payload.removed)
                page_cursor = payload.next_cursor
                if not payload.has_more:
                    break
        except Exception as exc:
            if _plaid_error_code(exc) != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" or attempt == 2:
                raise
            logger.warning("transactions/sync pagination changed for item %s; restarting from saved cursor", item_id)
            continue

        await storage.apply_transaction_delta(
            item_id=item_id,
            added=[txn.model_dump(mode="json", exclude_unset=True) for txn in added],
            modified=[txn.model_dump(mode="json", exclude_unset=True) for txn in modified],
            removed=[txn.model_dump(mode="json", exclude_unset=True) for txn in removed],
            next_cursor=page_cursor,
            captured_at=captured_at or datetime.now(UTC),
        )
        return


async def _fetch_investment_transactions(
    api: PlaidApiLike, storage: PlaidLinkStorage, run_id: UUID, access_token: str, item_id: str, start: date, end: date
) -> list[PlaidInvestmentTransaction]:
    offset = 0
    count = 500
    out: list[PlaidInvestmentTransaction] = []
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
            response_model=InvestmentsTransactionsGetResponse,
        )
        total = payload.total_investment_transactions
        page = payload.investment_transactions or []
        out.extend(page)
        offset += len(page)
        if not page:
            break
    return out


async def _call[PlaidRequestT: PlaidRequestLike, ResponseT: PlaidApiResponse](
    api: PlaidApiLike,
    storage: PlaidLinkStorage,
    run_id: UUID,
    endpoint: str,
    call: Callable[[PlaidRequestT], object],
    request: PlaidRequestT,
    item_id: str,
    *,
    response_model: type[ResponseT],
) -> ResponseT:
    started = time.monotonic()
    request_json = _request_json(request)
    try:
        response = await asyncio.to_thread(call, request)
        serialized = api.api_client.sanitize_for_serialization(response)
        typed_response = response_model.model_validate(serialized)
        response_json = typed_response.model_dump(mode="json", exclude_unset=True)
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
            request_id=_extract_request_id(typed_response),
            status="ok",
            duration_ms=int((time.monotonic() - started) * 1000),
            request_json=redact_payload(request_json),
            response_json=redact_payload(response_json),
        )
    )
    return typed_response


def _request_json(request: PlaidRequestLike) -> dict[str, Any]:
    return cast(dict[str, Any], request.to_dict())


def _plaid_error_code(exc: Exception) -> str | None:
    """Plaid's machine-readable error_code (e.g. NO_LIABILITY_ACCOUNTS), or the HTTP status."""
    if not isinstance(exc, PlaidApiException):
        return None
    try:
        parsed = json.loads(exc.body)
    except TypeError, ValueError:
        # Body absent or not JSON — not a structured Plaid error; fall through to the status.
        parsed = None
    if isinstance(parsed, dict) and isinstance(code := parsed.get("error_code"), str):
        return code
    return str(exc.status) if exc.status is not None else None


def _extract_request_id(response: PlaidApiResponse) -> str | None:
    return response.request_id
