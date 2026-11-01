from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance.plaid.db.link_store import ApiEvent, PlaidLinkStorage
from finance.plaid.db.models import HoldingSnapshotRow, async_session_factory
from finance.plaid.db.read_model import read_current_cash_balances, read_current_holdings
from finance.plaid.db.testing import ensure_test_sync_run


@pytest.fixture
async def session_factory(db_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine, factory = async_session_factory(db_url)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _capture_holdings(
    storage: PlaidLinkStorage,
    *,
    item_id: str,
    securities: Sequence[Mapping[str, object]],
    holdings: Sequence[Mapping[str, object]],
    current: Decimal,
    captured_at: datetime,
) -> None:
    api_event_id = await storage.record_api_event(
        ApiEvent(endpoint="investments/holdings/get", status="ok", item_id=item_id, request_json={}, response_json={})
    )
    await storage.apply_holdings(
        run_id=await ensure_test_sync_run(storage, item_id=item_id),
        item_id=item_id,
        securities=[dict(security) for security in securities],
        holdings=[dict(holding) for holding in holdings],
        accounts=[
            {
                "account_id": "brokerage",
                "name": "Brokerage",
                "type": "investment",
                "balances": {"current": current, "iso_currency_code": "USD"},
            }
        ],
        api_event_id=api_event_id,
        captured_at=captured_at,
    )


async def test_read_current_cash_balances_returns_latest_selected_usd_snapshot(
    storage: PlaidLinkStorage,
    add_link: Callable[[str], Awaitable[None]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_link("item-cash")
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-cash"),
        item_id="item-cash",
        accounts=[
            {
                "account_id": "checking",
                "name": "Checking",
                "type": "depository",
                "balances": {
                    "available": Decimal("100.0"),
                    "current": Decimal("110.0"),
                    "limit": None,
                    "iso_currency_code": "USD",
                },
            },
            {
                "account_id": "cad",
                "name": "CAD",
                "type": "depository",
                "balances": {
                    "available": Decimal("5.0"),
                    "current": Decimal("6.0"),
                    "limit": None,
                    "iso_currency_code": "CAD",
                },
            },
        ],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-cash"),
        item_id="item-cash",
        accounts=[
            {
                "account_id": "checking",
                "name": "Checking",
                "type": "depository",
                "balances": {
                    "available": Decimal("150.0"),
                    "current": Decimal("9007199254740993.01"),
                    "limit": None,
                    "iso_currency_code": "USD",
                },
            }
        ],
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    balances = await read_current_cash_balances(
        session_factory=session_factory, account_ids=("checking", "cad", "missing")
    )

    assert [balance.account_id for balance in balances] == ["checking"]
    assert balances[0].current == Decimal("9007199254740993.01")
    assert balances[0].available == Decimal("150.0")
    assert balances[0].institution_name == "Investment Test"


async def test_read_current_holdings_returns_latest_selected_usd_holdings(
    storage: PlaidLinkStorage,
    add_link: Callable[[str], Awaitable[None]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_link("item-investments")
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-investments"),
        item_id="item-investments",
        accounts=[
            {
                "account_id": "brokerage",
                "name": "Brokerage",
                "type": "investment",
                "balances": {
                    "available": None,
                    "current": Decimal("1000.0"),
                    "limit": None,
                    "iso_currency_code": "USD",
                },
            }
        ],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    securities = [
        {"security_id": "sec-voo", "name": "Vanguard 500", "ticker_symbol": "VOO", "type": "etf", "raw_json": {}},
        {"security_id": "sec-cad", "name": "CAD Fund", "ticker_symbol": "CADF", "type": "etf", "raw_json": {}},
    ]
    await _capture_holdings(
        storage,
        item_id="item-investments",
        securities=securities,
        holdings=[
            {
                "account_id": "brokerage",
                "security_id": "sec-voo",
                "quantity": Decimal("2.0"),
                "cost_basis": Decimal("700.0"),
                "institution_price": Decimal("400.0"),
                "institution_value": Decimal("800.0"),
                "iso_currency_code": "USD",
            },
            {
                "account_id": "brokerage",
                "security_id": "sec-cad",
                "quantity": Decimal("1.0"),
                "cost_basis": Decimal("10.0"),
                "institution_price": Decimal("11.0"),
                "institution_value": Decimal("11.0"),
                "iso_currency_code": "CAD",
            },
        ],
        current=Decimal("1000.0"),
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    await _capture_holdings(
        storage,
        item_id="item-investments",
        securities=securities,
        holdings=[
            {
                "account_id": "brokerage",
                "security_id": "sec-voo",
                "quantity": Decimal("3.0"),
                "cost_basis": Decimal("900.0"),
                "institution_price": Decimal("450.0"),
                "institution_value": Decimal("1350.0"),
                "iso_currency_code": "USD",
            }
        ],
        current=Decimal("1000.0"),
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    holdings = await read_current_holdings(session_factory=session_factory, account_ids=("brokerage", "missing"))

    assert [holding.security_id for holding in holdings] == ["sec-voo"]
    assert holdings[0].ticker_symbol == "VOO"
    assert holdings[0].quantity == Decimal("3.0")
    assert holdings[0].institution_value == Decimal("1350.0")
    assert holdings[0].cost_basis == Decimal("900.0")


async def test_read_current_holdings_uses_latest_capture_and_preserves_empty_coverage(
    storage: PlaidLinkStorage,
    add_link: Callable[[str], Awaitable[None]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_link("item-investments")
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-investments"),
        item_id="item-investments",
        accounts=[{"account_id": "brokerage", "name": "Brokerage", "type": "investment"}],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    securities = [
        {"security_id": "security-a", "name": "Security A"},
        {"security_id": "security-b", "name": "Security B"},
    ]
    await _capture_holdings(
        storage,
        item_id="item-investments",
        securities=securities,
        holdings=[
            {"account_id": "brokerage", "security_id": "security-a", "institution_value": Decimal("10.01")},
            {
                "account_id": "brokerage",
                "security_id": "security-b",
                "institution_value": Decimal("9007199254740993.01"),
            },
        ],
        current=Decimal("9007199254741003.02"),
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    await _capture_holdings(
        storage,
        item_id="item-investments",
        securities=[securities[1]],
        holdings=[
            {
                "account_id": "brokerage",
                "security_id": "security-b",
                "institution_value": Decimal("9007199254740993.01"),
            }
        ],
        current=Decimal("9007199254740993.01"),
        captured_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
    )
    async with session_factory() as session:
        session.add(
            HoldingSnapshotRow(
                account_id="brokerage",
                security_id="security-a",
                item_id="item-investments",
                captured_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
                quantity=Decimal(1),
                institution_value=Decimal("10.01"),
                iso_currency_code="USD",
                raw_json={},
            )
        )
        await session.commit()

    holdings = await read_current_holdings(session_factory=session_factory, account_ids=("brokerage",))

    assert [holding.security_id for holding in holdings] == ["security-b"]
    assert holdings[0].institution_value == Decimal("9007199254740993.01")

    await _capture_holdings(
        storage,
        item_id="item-investments",
        securities=[],
        holdings=[],
        current=Decimal("9007199254740993.01"),
        captured_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
    )

    assert await read_current_holdings(session_factory=session_factory, account_ids=("brokerage",)) == ()


async def test_read_current_holdings_fails_closed_on_unlinked_legacy_row(
    storage: PlaidLinkStorage,
    add_link: Callable[[str], Awaitable[None]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_link("item-investments")
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-investments"),
        item_id="item-investments",
        accounts=[{"account_id": "brokerage", "name": "Brokerage", "type": "investment"}],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    captured_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await _capture_holdings(
        storage,
        item_id="item-investments",
        securities=[{"security_id": "security-a", "name": "Security A"}],
        holdings=[{"account_id": "brokerage", "security_id": "security-a", "institution_value": Decimal("10.01")}],
        current=Decimal("10.01"),
        captured_at=captured_at,
    )
    async with session_factory() as session:
        session.add(
            HoldingSnapshotRow(
                account_id="brokerage",
                security_id="security-a",
                item_id="item-investments",
                captured_at=captured_at,
                quantity=Decimal(1),
                institution_value=Decimal("10.01"),
                iso_currency_code="USD",
                raw_json={},
            )
        )
        await session.commit()

    with pytest.raises(
        RuntimeError, match="Unapplied holding observations make Item 'item-investments' unsafe to read"
    ):
        await read_current_holdings(session_factory=session_factory, account_ids=("brokerage",))


async def test_read_current_holdings_fails_closed_on_unapplied_successful_api_event(
    storage: PlaidLinkStorage,
    add_link: Callable[[str], Awaitable[None]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_link("item-investments")
    await storage.apply_accounts(
        run_id=await ensure_test_sync_run(storage, item_id="item-investments"),
        item_id="item-investments",
        accounts=[{"account_id": "brokerage", "name": "Brokerage", "type": "investment"}],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    await _capture_holdings(
        storage,
        item_id="item-investments",
        securities=[{"security_id": "security-a", "name": "Security A"}],
        holdings=[{"account_id": "brokerage", "security_id": "security-a", "institution_value": Decimal("10.01")}],
        current=Decimal("10.01"),
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    await storage.record_api_event(
        ApiEvent(
            endpoint="investments/holdings/get",
            status="ok",
            item_id="item-investments",
            request_json={},
            response_json={"accounts": [], "holdings": [], "securities": []},
        )
    )

    with pytest.raises(
        RuntimeError, match="Unapplied holding observations make Item 'item-investments' unsafe to read"
    ):
        await read_current_holdings(session_factory=session_factory, account_ids=("brokerage",))

    await _capture_holdings(
        storage,
        item_id="item-investments",
        securities=[{"security_id": "security-a", "name": "Security A"}],
        holdings=[{"account_id": "brokerage", "security_id": "security-a", "institution_value": Decimal("11.02")}],
        current=Decimal("11.02"),
        captured_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
    )

    holdings = await read_current_holdings(session_factory=session_factory, account_ids=("brokerage",))
    assert [holding.security_id for holding in holdings] == ["security-a"]
    assert holdings[0].institution_value == Decimal("11.02")


async def test_superseded_run_receipt_does_not_hide_successor_holdings(
    storage: PlaidLinkStorage,
    add_link: Callable[[str], Awaitable[None]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_link("item-investments")
    stale_run_id = await storage.begin_sync_run(trigger="cron", item_id="item-investments", configured_windows={})
    async with session_factory() as session:
        await session.execute(
            text("UPDATE sync_runs SET started_at = :started_at WHERE run_id = :run_id"),
            {"started_at": datetime.now(UTC) - timedelta(hours=3), "run_id": stale_run_id},
        )
        await session.commit()
    successor_run_id = await storage.begin_sync_run(
        trigger="webhook", item_id="item-investments", configured_windows={}
    )
    await storage.apply_accounts(
        run_id=successor_run_id,
        item_id="item-investments",
        accounts=[{"account_id": "brokerage", "name": "Brokerage", "type": "investment"}],
        captured_at=datetime.now(UTC),
    )
    event_id = await storage.record_api_event(
        ApiEvent(
            sync_run_id=successor_run_id,
            endpoint="investments/holdings/get",
            status="ok",
            item_id="item-investments",
            request_json={},
            response_json={},
        )
    )
    await storage.apply_holdings(
        run_id=successor_run_id,
        item_id="item-investments",
        api_event_id=event_id,
        securities=[{"security_id": "security-current", "name": "Current Security"}],
        holdings=[
            {"account_id": "brokerage", "security_id": "security-current", "institution_value": Decimal("12.34")}
        ],
        accounts=[
            {
                "account_id": "brokerage",
                "name": "Brokerage",
                "type": "investment",
                "balances": {"current": Decimal("12.34"), "iso_currency_code": "USD"},
            }
        ],
        captured_at=datetime.now(UTC),
    )
    stale_event_id = await storage.record_api_event(
        ApiEvent(
            sync_run_id=stale_run_id,
            endpoint="investments/holdings/get",
            status="ok",
            item_id="item-investments",
            request_json={},
            response_json={},
        )
    )

    holdings = await read_current_holdings(session_factory=session_factory, account_ids=("brokerage",))
    assert [holding.security_id for holding in holdings] == ["security-current"]
    async with session_factory() as session:
        assert (
            await session.execute(text("SELECT status FROM plaid_api_events WHERE id = :id"), {"id": stale_event_id})
        ).scalar_one() == "ok_superseded"


if __name__ == "__main__":
    pytest_bazel.main()
