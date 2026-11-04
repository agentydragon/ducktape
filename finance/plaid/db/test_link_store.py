from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finance.plaid.db.link_store import ApiEvent, PlaidLinkStorage, SyncAlreadyRunningError, SyncLeaseLostError
from finance.plaid.db.models import ItemSyncQueueRow
from finance.plaid.db.testing import ensure_test_sync_run
from util.db_url import render_postgresql_url


async def _record_holdings_event(storage: PlaidLinkStorage, item_id: str) -> int:
    return await storage.record_api_event(
        ApiEvent(
            endpoint="investments/holdings/get",
            item_id=item_id,
            status="ok",
            request_json={},
            response_json={"accounts": [], "securities": [], "holdings": []},
        )
    )


async def test_successful_sync_run_marks_investment_only_link_synced(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]]
) -> None:
    await add_link("item-investments")
    before = datetime.now(UTC)

    run_id = await storage.begin_sync_run(trigger="link", item_id="item-investments", configured_windows={})
    await storage.finish_sync_run(run_id, status="succeeded")

    link = await storage.get_link("item-investments")
    assert link is not None
    assert link.last_synced_at is not None
    assert link.last_synced_at >= before


async def test_failed_sync_run_does_not_mark_link_synced(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]]
) -> None:
    await add_link("item-investments")

    run_id = await storage.begin_sync_run(trigger="link", item_id="item-investments", configured_windows={})
    await storage.finish_sync_run(run_id, status="failed", error_summary="boom")

    link = await storage.get_link("item-investments")
    assert link is not None
    assert link.last_synced_at is None


async def test_spend_changed_notifies_after_commit_and_on_link_removal(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]], db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_link("item-investments")
    listener_url = render_postgresql_url(db_url, "postgresql")
    listener = await asyncpg.connect(listener_url)
    notifications: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    await listener.add_listener(
        "plaid_spend_changed", lambda _connection, _pid, channel, payload: notifications.put_nowait((channel, payload))
    )
    allow_commit = asyncio.Event()
    finish_task: asyncio.Task[None] | None = None

    async def assert_no_notification() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(notifications.get(), timeout=0.05)

    try:
        run_id = await storage.begin_sync_run(trigger="test", item_id="item-investments", configured_windows={})
        commit_entered = asyncio.Event()
        original_commit = AsyncSession.commit

        async def paused_commit(session: AsyncSession) -> None:
            commit_entered.set()
            await allow_commit.wait()
            await original_commit(session)

        with monkeypatch.context() as patcher:
            patcher.setattr(AsyncSession, "commit", paused_commit)
            finish_task = asyncio.create_task(storage.finish_sync_run(run_id, status="succeeded"))
            await asyncio.wait_for(commit_entered.wait(), timeout=5)
            await assert_no_notification()
            allow_commit.set()
            await finish_task

        assert await asyncio.wait_for(notifications.get(), timeout=5) == ("plaid_spend_changed", "")
        assert await listener.fetchval("SELECT status FROM sync_runs WHERE run_id = $1", run_id) == "succeeded"

        failed_run_id = await storage.begin_sync_run(trigger="test", item_id="item-investments", configured_windows={})
        await storage.finish_sync_run(failed_run_id, status="failed", error_summary="expected failure")
        await storage.purge_link_data("missing-item")
        await assert_no_notification()

        await storage.apply_accounts(
            run_id=await ensure_test_sync_run(storage, item_id="item-investments"),
            item_id="item-investments",
            accounts=[{"account_id": "account-investments", "name": "Brokerage", "type": "investment"}],
            captured_at=datetime.now(UTC),
        )
        assert await asyncio.wait_for(notifications.get(), timeout=5) == ("plaid_spend_changed", "")
        event_id = await _record_holdings_event(storage, "item-investments")
        await storage.apply_holdings(
            run_id=await ensure_test_sync_run(storage, item_id="item-investments"),
            item_id="item-investments",
            accounts=[{"account_id": "account-investments", "name": "Brokerage", "type": "investment"}],
            securities=[],
            holdings=[],
            api_event_id=event_id,
            captured_at=datetime.now(UTC),
        )
        assert await asyncio.wait_for(notifications.get(), timeout=5) == ("plaid_spend_changed", "")
        assert await listener.fetchval("SELECT count(*) FROM holding_captures") == 1
        await storage.purge_link_data("item-investments")
        assert await asyncio.wait_for(notifications.get(), timeout=5) == ("plaid_spend_changed", "")
        assert await listener.fetchval("SELECT count(*) FROM links WHERE item_id = $1", "item-investments") == 0
        assert (
            await listener.fetchval("SELECT count(*) FROM accounts WHERE account_id = $1", "account-investments") == 0
        )
        await assert_no_notification()
    finally:
        allow_commit.set()
        if finish_task is not None and not finish_task.done():
            await asyncio.gather(finish_task, return_exceptions=True)
        await listener.close()


async def test_transaction_delta_commits_changes_and_cursor_together(
    storage: PlaidLinkStorage, engine: AsyncEngine
) -> None:
    await storage.upsert_link(
        run_id=None,
        item_id="item-transactions",
        access_token_secret="item-transactions-token",
        products_requested=["transactions"],
        institution_id="ins_transactions",
        institution_name="Transactions Test",
        label=None,
    )

    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-transactions"),
        item_id="item-transactions",
        accounts=[{"account_id": "account-transactions", "name": "Checking", "type": "depository", "balances": {}}],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    captured_at = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    await storage.apply_transaction_delta(
        run_id=await ensure_test_sync_run(storage, item_id="item-transactions"),
        item_id="item-transactions",
        added=[
            {
                "transaction_id": "txn-kept",
                "account_id": "account-transactions",
                "date": "2026-05-30",
                "amount": 12.34,
                "name": "Coffee",
                "pending": False,
            },
            {
                "transaction_id": "txn-removed",
                "account_id": "account-transactions",
                "date": "2026-05-30",
                "amount": 9.0,
                "name": "Old charge",
                "pending": False,
            },
        ],
        modified=[],
        removed=[],
        next_cursor="cursor-first",
        captured_at=captured_at,
    )
    await storage.apply_transaction_delta(
        run_id=await ensure_test_sync_run(storage, item_id="item-transactions"),
        item_id="item-transactions",
        added=[],
        modified=[
            {
                "transaction_id": "txn-kept",
                "account_id": "account-transactions",
                "date": "2026-05-30",
                "amount": 10.0,
                "name": "Updated Coffee",
                "pending": False,
            }
        ],
        removed=[{"transaction_id": "txn-removed"}],
        next_cursor="cursor-second",
        captured_at=captured_at,
    )

    link = await storage.get_link("item-transactions")
    assert link is not None
    assert link.last_synced_at is None
    assert link.transactions_cursor == "cursor-second"
    async with engine.connect() as conn:
        kept = (
            await conn.execute(text("SELECT name, amount, removed FROM transactions WHERE transaction_id = 'txn-kept'"))
        ).one()
        removed = (
            await conn.execute(text("SELECT removed FROM transactions WHERE transaction_id = 'txn-removed'"))
        ).scalar_one()
    assert kept == ("Updated Coffee", 10.0, False)
    assert removed is True


async def test_transaction_foreign_key_rejects_account_from_another_item(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]], engine: AsyncEngine
) -> None:
    await add_link("item-account-owner")
    await add_link("item-transaction-owner")
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-account-owner"),
        item_id="item-account-owner",
        accounts=[{"account_id": "account-owned", "name": "Checking", "type": "depository"}],
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(transaction_id, account_id, item_id, date, amount, name, pending, raw_json, updated_at) "
                    "VALUES ('txn-cross-item', 'account-owned', 'item-transaction-owner', "
                    "'2026-06-01', 1, 'Cross Item', false, '{}'::jsonb, now())"
                )
            )


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity"])
async def test_transaction_numeric_check_rejects_nonfinite_direct_sql_values(
    amount: str, storage: PlaidLinkStorage, engine: AsyncEngine
) -> None:
    await storage.upsert_link(
        run_id=None,
        item_id="item-nonfinite-amount",
        access_token_secret="item-nonfinite-amount-token",
        products_requested=["transactions"],
        institution_id="ins_transactions",
        institution_name="Transactions Test",
        label=None,
    )
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-nonfinite-amount"),
        item_id="item-nonfinite-amount",
        accounts=[{"account_id": "account-nonfinite-amount", "name": "Checking", "type": "depository"}],
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(transaction_id, account_id, item_id, date, amount, name, pending, raw_json, updated_at) "
                    "VALUES (:transaction_id, 'account-nonfinite-amount', 'item-nonfinite-amount', "
                    "'2026-06-01', CAST(:amount AS numeric), 'Nonfinite', false, '{}'::jsonb, now())"
                ),
                {"transaction_id": f"txn-nonfinite-{amount}", "amount": amount},
            )


async def test_apply_holdings_records_empty_capture_and_covered_empty_account(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]], engine: AsyncEngine
) -> None:
    captured_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await add_link("item-empty-holdings")
    await add_link("item-covered-holdings")
    empty_event_id = await _record_holdings_event(storage, "item-empty-holdings")
    await storage.apply_holdings(
        run_id=await ensure_test_sync_run(storage, item_id="item-empty-holdings"),
        item_id="item-empty-holdings",
        api_event_id=empty_event_id,
        accounts=[],
        securities=[],
        holdings=[],
        captured_at=captured_at,
    )

    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-covered-holdings"),
        item_id="item-covered-holdings",
        accounts=[
            {"account_id": "account-no-positions", "name": "Retirement", "type": "investment"},
            {
                "account_id": "account-with-position",
                "name": "Brokerage",
                "type": "investment",
                "balances": {"current": 123.45, "iso_currency_code": "USD"},
            },
        ],
        captured_at=captured_at,
    )
    covered_event_id = await _record_holdings_event(storage, "item-covered-holdings")
    await storage.apply_holdings(
        run_id=await ensure_test_sync_run(storage, item_id="item-covered-holdings"),
        item_id="item-covered-holdings",
        api_event_id=covered_event_id,
        accounts=[
            {"account_id": "account-no-positions", "name": "Retirement", "type": "investment"},
            {
                "account_id": "account-with-position",
                "name": "Brokerage",
                "type": "investment",
                "balances": {"current": 123.45, "iso_currency_code": "USD"},
            },
        ],
        securities=[{"security_id": "security-one", "name": "One", "raw_json": {}}],
        holdings=[{"account_id": "account-with-position", "security_id": "security-one", "quantity": 1}],
        captured_at=captured_at,
    )

    async with engine.connect() as conn:
        captures = (
            await conn.execute(text("SELECT id, item_id, api_event_id FROM holding_captures ORDER BY item_id"))
        ).all()
        account_observations = (
            await conn.execute(
                text(
                    "SELECT capture_id, account_id, current, iso_currency_code "
                    "FROM holding_capture_accounts ORDER BY account_id"
                )
            )
        ).all()
        holding_rows = (await conn.execute(text("SELECT capture_id, account_id FROM holding_snapshots"))).all()
    assert len(captures) == 2
    assert [capture[1] for capture in captures] == ["item-covered-holdings", "item-empty-holdings"]
    assert [capture[2] for capture in captures] == [covered_event_id, empty_event_id]
    assert captures[0][0] != captures[1][0]
    assert [observation[0] for observation in account_observations] == [captures[0][0], captures[0][0]]
    assert [observation[1] for observation in account_observations] == ["account-no-positions", "account-with-position"]
    assert [observation[2] for observation in account_observations] == [None, Decimal("123.45")]
    assert [observation[3] for observation in account_observations] == [None, "USD"]
    assert len(holding_rows) == 1
    assert holding_rows[0][0] == captures[0][0]
    assert holding_rows[0][1] == "account-with-position"


async def test_apply_holdings_rolls_back_if_a_snapshot_row_is_invalid(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]], engine: AsyncEngine
) -> None:
    captured_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await add_link("item-invalid-holdings")
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-invalid-holdings"),
        item_id="item-invalid-holdings",
        accounts=[{"account_id": "account-invalid-holdings", "name": "Brokerage", "type": "investment"}],
        captured_at=captured_at,
    )
    event_id = await _record_holdings_event(storage, "item-invalid-holdings")

    with pytest.raises(IntegrityError):
        await storage.apply_holdings(
            run_id=await ensure_test_sync_run(storage, item_id="item-invalid-holdings"),
            item_id="item-invalid-holdings",
            api_event_id=event_id,
            accounts=[{"account_id": "account-invalid-holdings", "name": "Brokerage", "type": "investment"}],
            securities=[{"security_id": "security-rollback", "name": "Rollback", "raw_json": {}}],
            holdings=[
                {"account_id": "account-invalid-holdings", "security_id": "security-rollback", "quantity": 1},
                {"account_id": "account-invalid-holdings", "security_id": "security-rollback", "quantity": 2},
            ],
            captured_at=captured_at,
        )

    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM holding_captures WHERE item_id = 'item-invalid-holdings'"))
        ).scalar_one() == 0
        assert (
            await conn.execute(
                text("SELECT count(*) FROM holding_capture_accounts WHERE item_id = 'item-invalid-holdings'")
            )
        ).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT count(*) FROM holding_snapshots WHERE item_id = 'item-invalid-holdings'"))
        ).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT count(*) FROM securities WHERE security_id = 'security-rollback'"))
        ).scalar_one() == 0


async def test_delete_detached_account_removes_its_capture_rows_and_keeps_item_provenance(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]], engine: AsyncEngine
) -> None:
    first_observation = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    later_observation = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    item_id = "item-detached-capture"
    await add_link(item_id)
    first_accounts = [
        {"account_id": "account-detached", "name": "Old Brokerage", "type": "investment"},
        {"account_id": "account-retained", "name": "Current Brokerage", "type": "investment"},
    ]
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id=item_id),
        item_id=item_id,
        accounts=first_accounts,
        captured_at=first_observation,
    )
    event_id = await _record_holdings_event(storage, item_id)
    await storage.apply_holdings(
        run_id=await ensure_test_sync_run(storage, item_id=item_id),
        item_id=item_id,
        api_event_id=event_id,
        accounts=first_accounts,
        securities=[
            {"security_id": "security-detached", "name": "Old Fund", "raw_json": {}},
            {"security_id": "security-retained", "name": "Current Fund", "raw_json": {}},
        ],
        holdings=[
            {"account_id": "account-detached", "security_id": "security-detached", "quantity": 1},
            {"account_id": "account-retained", "security_id": "security-retained", "quantity": 2},
        ],
        captured_at=first_observation,
    )
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id=item_id),
        item_id=item_id,
        accounts=[first_accounts[1]],
        captured_at=later_observation,
    )
    await storage.finish_sync_run(await ensure_test_sync_run(storage, item_id=item_id), status="succeeded")
    preview = await storage.get_account_deletion_preview(item_id=item_id, account_id="account-detached")

    await storage.delete_detached_account(item_id=item_id, account_id="account-detached", expected_preview=preview)

    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM accounts WHERE account_id = 'account-detached'"))
        ).scalar_one() == 0
        captures = (
            await conn.execute(
                text("SELECT id, api_event_id FROM holding_captures WHERE item_id = :item_id"), {"item_id": item_id}
            )
        ).all()
        covered_accounts = (
            (
                await conn.execute(
                    text("SELECT account_id FROM holding_capture_accounts WHERE item_id = :item_id"),
                    {"item_id": item_id},
                )
            )
            .scalars()
            .all()
        )
        holdings = (
            (
                await conn.execute(
                    text("SELECT account_id FROM holding_snapshots WHERE item_id = :item_id"), {"item_id": item_id}
                )
            )
            .scalars()
            .all()
        )
        assert (
            await conn.execute(
                text("SELECT count(*) FROM plaid_api_events WHERE id = :event_id"), {"event_id": event_id}
            )
        ).scalar_one() == 1
    assert len(captures) == 1
    assert captures[0][1] == event_id
    assert covered_accounts == ["account-retained"]
    assert holdings == ["account-retained"]


async def test_sync_run_claim_is_unique_per_item(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]]
) -> None:
    await add_link("item-investments")
    await storage.begin_sync_run(trigger="cron", item_id="item-investments", configured_windows={})

    with pytest.raises(SyncAlreadyRunningError):
        await storage.begin_sync_run(trigger="webhook", item_id="item-investments", configured_windows={})


async def test_sync_run_claim_recovers_a_stale_run(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]], engine: AsyncEngine
) -> None:
    await add_link("item-investments")
    stale_run_id = await storage.begin_sync_run(trigger="cron", item_id="item-investments", configured_windows={})
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sync_runs SET started_at = :started_at WHERE run_id = :run_id"),
            {"started_at": datetime.now(UTC) - timedelta(hours=3), "run_id": stale_run_id},
        )

    recovered_run_id = await storage.begin_sync_run(
        trigger="webhook", item_id="item-investments", configured_windows={}
    )
    async with engine.connect() as conn:
        old_status = (
            await conn.execute(text("SELECT status FROM sync_runs WHERE run_id = :run_id"), {"run_id": stale_run_id})
        ).scalar_one()
        new_status = (
            await conn.execute(
                text("SELECT status FROM sync_runs WHERE run_id = :run_id"), {"run_id": recovered_run_id}
            )
        ).scalar_one()
    assert old_status == "failed"
    assert new_status == "running"


async def test_recovered_sync_cannot_rewind_transaction_cursor(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]], engine: AsyncEngine
) -> None:
    await add_link("item-investments")
    stale_run_id = await storage.begin_sync_run(trigger="cron", item_id="item-investments", configured_windows={})
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sync_runs SET started_at = :started_at WHERE run_id = :run_id"),
            {"started_at": datetime.now(UTC) - timedelta(hours=3), "run_id": stale_run_id},
        )
    recovered_run_id = await storage.begin_sync_run(
        trigger="webhook", item_id="item-investments", configured_windows={}
    )
    await storage.apply_transaction_delta(
        run_id=recovered_run_id,
        item_id="item-investments",
        added=[],
        modified=[],
        removed=[],
        next_cursor="fresh",
        captured_at=datetime.now(UTC),
    )
    with pytest.raises(SyncLeaseLostError):
        await storage.apply_transaction_delta(
            run_id=stale_run_id,
            item_id="item-investments",
            added=[],
            modified=[],
            removed=[],
            next_cursor="stale",
            captured_at=datetime.now(UTC),
        )
    with pytest.raises(SyncLeaseLostError):
        await storage.finish_sync_run(stale_run_id, status="succeeded")
    link = await storage.get_link("item-investments")
    assert link is not None
    assert link.transactions_cursor == "fresh"
    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT status FROM sync_runs WHERE run_id = :run_id"), {"run_id": stale_run_id})
        ).scalar_one() == "failed"


async def test_api_receipt_commits_before_takeover_waiting_on_same_item_lock(
    storage: PlaidLinkStorage,
    add_link: Callable[[str], Awaitable[None]],
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_link("item-receipt")
    stale_run_id = await storage.begin_sync_run(trigger="cron", item_id="item-receipt", configured_windows={})
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sync_runs SET started_at = :started_at WHERE run_id = :run_id"),
            {"started_at": datetime.now(UTC) - timedelta(hours=3), "run_id": stale_run_id},
        )
    commit_entered = asyncio.Event()
    allow_commit = asyncio.Event()
    original_commit = AsyncSession.commit

    async def paused_commit(session: AsyncSession) -> None:
        commit_entered.set()
        await allow_commit.wait()
        await original_commit(session)

    with monkeypatch.context() as patcher:
        patcher.setattr(AsyncSession, "commit", paused_commit)
        receipt_task = asyncio.create_task(
            storage.record_api_event(
                ApiEvent(
                    sync_run_id=stale_run_id,
                    endpoint="investments/holdings/get",
                    item_id="item-receipt",
                    status="ok",
                    request_json={},
                    response_json={},
                )
            )
        )
        await asyncio.wait_for(commit_entered.wait(), timeout=5)
        successor_task = asyncio.create_task(
            storage.begin_sync_run(trigger="webhook", item_id="item-receipt", configured_windows={})
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(successor_task), timeout=0.05)
        allow_commit.set()
        event_id = await receipt_task
        await successor_task

    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT status FROM plaid_api_events WHERE id = :id"), {"id": event_id})
        ).scalar_one() == "ok"


async def test_item_sync_queue_coalesces_events_during_a_claim(storage: PlaidLinkStorage) -> None:
    await storage.upsert_link(
        run_id=None,
        item_id="item-transactions",
        access_token_secret="item-transactions-token",
        products_requested=["transactions"],
        institution_id="ins_transactions",
        institution_name="Transactions Test",
        label=None,
    )
    await storage.enqueue_item_sync("item-transactions")
    first_claim = await storage.claim_item_sync()
    assert first_claim is not None
    await storage.enqueue_item_sync("item-transactions")
    await storage.finish_item_sync(first_claim)

    second_claim = await storage.claim_item_sync()
    assert second_claim is not None
    assert second_claim.generation == first_claim.generation + 1
    await storage.finish_item_sync(second_claim)
    assert await storage.claim_item_sync() is None


async def test_queue_retry_cannot_release_a_reclaimed_claim(
    storage: PlaidLinkStorage,
    add_link: Callable[[str], Awaitable[None]],
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_link("item-queue-race")
    await storage.enqueue_item_sync("item-queue-race")
    first_claim = await storage.claim_item_sync()
    assert first_claim is not None
    await storage.enqueue_item_sync("item-queue-race")
    old_claimed_at = datetime.now(UTC) - timedelta(minutes=31)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE item_sync_queue SET claimed_at = :claimed_at WHERE item_id = :item_id"),
            {"claimed_at": old_claimed_at, "item_id": first_claim.item_id},
        )
    first_claim = replace(first_claim, claimed_at=old_claimed_at)
    retry_read = asyncio.Event()
    allow_retry = asyncio.Event()
    original_get = AsyncSession.get

    async def paused_get(session: AsyncSession, entity: Any, ident: Any, **kwargs: Any) -> Any:
        row = await original_get(session, entity, ident, **kwargs)
        if entity is ItemSyncQueueRow and ident == first_claim.item_id:
            retry_read.set()
            await allow_retry.wait()
        return row

    with monkeypatch.context() as patcher:
        patcher.setattr(AsyncSession, "get", paused_get)
        retry_task = asyncio.create_task(storage.retry_item_sync(first_claim))
        await asyncio.wait_for(retry_read.wait(), timeout=5)
        try:
            assert await storage.claim_item_sync() is None
        finally:
            allow_retry.set()
        await retry_task
        successor_claim = await storage.claim_item_sync()

    assert successor_claim is not None
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT claimed_at FROM item_sync_queue WHERE item_id = :item_id"),
                {"item_id": first_claim.item_id},
            )
        ).scalar_one() == successor_claim.claimed_at


async def test_plaid_webhook_delivery_keeps_full_body_and_dispatch_metadata(
    storage: PlaidLinkStorage, engine: AsyncEngine
) -> None:
    raw_body = '{"webhook_type":"ITEM","webhook_code":"WEBHOOK_UPDATE_ACKNOWLEDGED","extra":{"value":42}}'
    delivery_id = await storage.record_plaid_webhook_delivery(raw_body)
    await storage.update_plaid_webhook_delivery(
        delivery_id,
        webhook_type="ITEM",
        webhook_code="WEBHOOK_UPDATE_ACKNOWLEDGED",
        item_id=None,
        disposition="ignored",
    )

    async with engine.connect() as conn:
        delivery = (
            (
                await conn.execute(
                    text(
                        "SELECT raw_body, webhook_type, webhook_code, item_id, disposition "
                        "FROM plaid_webhook_deliveries WHERE id = :delivery_id"
                    ),
                    {"delivery_id": delivery_id},
                )
            )
            .mappings()
            .one()
        )
    assert delivery == {
        "raw_body": raw_body,
        "webhook_type": "ITEM",
        "webhook_code": "WEBHOOK_UPDATE_ACKNOWLEDGED",
        "item_id": None,
        "disposition": "ignored",
    }


async def test_purge_link_data_removes_mirrored_rows_but_keeps_audit_history(
    storage: PlaidLinkStorage, add_link: Callable[[str], Awaitable[None]], engine: AsyncEngine
) -> None:
    captured_at = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    await add_link("item-purge")
    await add_link("item-keep")
    await storage.enqueue_item_sync("item-purge")

    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-purge"),
        item_id="item-purge",
        accounts=[
            {
                "account_id": "account-purge",
                "name": "Purge Checking",
                "type": "depository",
                "balances": {"available": 10.0, "current": 11.0, "limit": None, "iso_currency_code": "USD"},
            }
        ],
        captured_at=captured_at,
    )
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-keep"),
        item_id="item-keep",
        accounts=[
            {
                "account_id": "account-keep",
                "name": "Keep Brokerage",
                "type": "investment",
                "balances": {"available": None, "current": 20.0, "limit": None, "iso_currency_code": "USD"},
            }
        ],
        captured_at=captured_at,
    )
    await storage.apply_transaction_delta(
        run_id=await ensure_test_sync_run(storage, item_id="item-purge"),
        item_id="item-purge",
        added=[
            {
                "transaction_id": "txn-purge",
                "account_id": "account-purge",
                "date": "2026-05-30",
                "amount": 12.34,
                "name": "Coffee",
                "pending": False,
            }
        ],
        modified=[],
        removed=[],
        next_cursor="cursor-purge",
        captured_at=captured_at,
    )
    purge_holding_event_id = await _record_holdings_event(storage, "item-purge")
    await storage.apply_holdings(
        run_id=await ensure_test_sync_run(storage, item_id="item-purge"),
        item_id="item-purge",
        api_event_id=purge_holding_event_id,
        accounts=[
            {
                "account_id": "account-purge",
                "name": "Purge Checking",
                "type": "depository",
                "balances": {"current": 11.0, "iso_currency_code": "USD"},
            }
        ],
        securities=[
            {"security_id": "security-purge", "name": "Purge Fund", "raw_json": {}},
            {"security_id": "security-shared", "name": "Shared Fund", "raw_json": {}},
        ],
        holdings=[
            {"account_id": "account-purge", "security_id": "security-purge", "quantity": 1.0},
            {"account_id": "account-purge", "security_id": "security-shared", "quantity": 2.0},
        ],
        captured_at=captured_at,
    )
    keep_holding_event_id = await _record_holdings_event(storage, "item-keep")
    await storage.apply_holdings(
        run_id=await ensure_test_sync_run(storage, item_id="item-keep"),
        item_id="item-keep",
        api_event_id=keep_holding_event_id,
        accounts=[
            {
                "account_id": "account-keep",
                "name": "Keep Brokerage",
                "type": "investment",
                "balances": {"current": 20.0, "iso_currency_code": "USD"},
            }
        ],
        securities=[{"security_id": "security-shared", "name": "Shared Fund", "raw_json": {}}],
        holdings=[{"account_id": "account-keep", "security_id": "security-shared", "quantity": 3.0}],
        captured_at=captured_at,
    )
    await storage.upsert_investment_transactions(
        run_id=await ensure_test_sync_run(storage, item_id="item-purge"),
        item_id="item-purge",
        transactions=[
            {
                "investment_transaction_id": "investment-txn-purge",
                "account_id": "account-purge",
                "security_id": "security-purge",
                "date": "2026-05-30",
            }
        ],
        captured_at=captured_at,
    )
    await storage.append_liability_snapshots(
        run_id=await ensure_test_sync_run(storage, item_id="item-purge"),
        item_id="item-purge",
        liabilities={
            "credit": [{"account_id": "account-purge", "raw_json": {"kind": "credit"}}],
            "mortgage": [{"account_id": "account-purge", "raw_json": {"kind": "mortgage"}}],
            "student": [{"account_id": "account-purge", "raw_json": {"kind": "student"}}],
        },
        captured_at=captured_at,
    )
    run_id = await ensure_test_sync_run(storage, item_id="item-purge")
    await storage.record_api_event(
        ApiEvent(
            sync_run_id=run_id,
            endpoint="transactions/get",
            item_id="item-purge",
            status="ok",
            request_json={"item_id": "item-purge"},
            response_json={"transactions": []},
        )
    )

    await storage.purge_link_data("item-purge")

    async with engine.connect() as conn:
        for table in (
            "links",
            "accounts",
            "transactions",
            "balance_snapshots",
            "holding_snapshots",
            "investment_transactions",
            "liability_credit_snapshots",
            "liability_mortgage_snapshots",
            "liability_student_snapshots",
        ):
            assert (
                await conn.execute(text(f"SELECT count(*) FROM {table} WHERE item_id = 'item-purge'"))
            ).scalar_one() == 0
        assert (await conn.execute(text("SELECT count(*) FROM links WHERE item_id = 'item-keep'"))).scalar_one() == 1
        assert (
            await conn.execute(text("SELECT count(*) FROM holding_captures WHERE item_id = 'item-purge'"))
        ).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT count(*) FROM holding_capture_accounts WHERE item_id = 'item-purge'"))
        ).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT count(*) FROM holding_captures WHERE item_id = 'item-keep'"))
        ).scalar_one() == 1
        assert (
            await conn.execute(text("SELECT count(*) FROM securities WHERE security_id = 'security-purge'"))
        ).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT count(*) FROM securities WHERE security_id = 'security-shared'"))
        ).scalar_one() == 1
        assert (
            await conn.execute(text("SELECT count(*) FROM sync_runs WHERE item_id = 'item-purge'"))
        ).scalar_one() == 1
        assert (
            await conn.execute(text("SELECT count(*) FROM item_sync_queue WHERE item_id = 'item-purge'"))
        ).scalar_one() == 0
        assert (
            await conn.execute(text("SELECT count(*) FROM plaid_api_events WHERE item_id = 'item-purge'"))
        ).scalar_one() == 2


if __name__ == "__main__":
    pytest_bazel.main()
