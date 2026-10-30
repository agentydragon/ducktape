from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_bazel
from sqlalchemy.exc import IntegrityError

from finance.plaid.db.models import AccountRow, LinkRow, TransactionRow, async_session_factory
from finance.plaid.transaction_queries import select_transactions


async def test_selected_retained_active_attached_rows(db_url: str) -> None:
    engine, sessions = async_session_factory(db_url)
    try:
        async with sessions() as session:
            session.add_all(
                [
                    LinkRow(
                        item_id=item,
                        products_requested=[],
                        products_authorized=[],
                        products_billed=[],
                        status=status,
                        access_token_secret="synthetic-token",
                    )
                    for item, status in [("active-item", "active"), ("inactive-item", "revoked")]
                ]
            )
            await session.flush()
            session.add_all(
                [
                    AccountRow(
                        account_id=account, item_id=item, name=account, type="credit", raw_json={}, detached_at=detached
                    )
                    for account, item, detached in [
                        ("chosen", "active-item", None),
                        ("detached", "active-item", datetime(2026, 1, 1, tzinfo=UTC)),
                        ("inactive", "inactive-item", None),
                        ("unselected", "active-item", None),
                    ]
                ]
            )
            await session.flush()
            session.add_all(
                [
                    TransactionRow(
                        transaction_id=identity,
                        account_id=account,
                        item_id=item,
                        date=date(2026, 1, 2),
                        amount=Decimal("12.5"),
                        name="Synthetic purchase",
                        pending=pending,
                        removed=removed,
                        iso_currency_code="USD",
                        raw_json={
                            "authorized_date": "2026-01-01",
                            "authorized_datetime": "2026-01-01T15:00:00Z",
                            "datetime": "2026-01-02T15:00:00Z",
                            "merchant_category_code": "1234",
                            "personal_finance_category": {"confidence_level": "HIGH"},
                            "counterparties": [{"name": "Synthetic merchant"}] if not pending else None,
                        },
                    )
                    for identity, account, item, pending, removed in [
                        ("posted", "chosen", "active-item", False, False),
                        ("pending", "chosen", "active-item", True, False),
                        ("removed", "chosen", "active-item", False, True),
                        ("detached-row", "detached", "active-item", False, False),
                        ("inactive-row", "inactive", "inactive-item", False, False),
                        ("unselected-row", "unselected", "active-item", False, False),
                    ]
                ]
            )
            await session.commit()
            session.add(
                TransactionRow(
                    transaction_id="wrong-item",
                    account_id="chosen",
                    item_id="inactive-item",
                    date=date(2026, 1, 2),
                    amount=Decimal("12.5"),
                    name="Synthetic ownership mismatch",
                    pending=False,
                    removed=False,
                    raw_json={},
                )
            )
            with pytest.raises(IntegrityError, match="fk_transactions_account_item"):
                await session.flush()
            await session.rollback()
            rows = (await session.execute(select_transactions(["chosen", "detached", "inactive"]))).mappings().all()
            assert {row["transaction_id"] for row in rows} == {"posted", "pending"}
            posted = next(row for row in rows if row["transaction_id"] == "posted")
            assert isinstance(posted["amount"], str)
            assert Decimal(posted["amount"]) == Decimal("12.5")
            assert posted["currency"] == "USD"
            assert posted["date"] == "2026-01-02"
            assert posted["authorized_date"] == "2026-01-01"
            assert posted["merchant_category_code"] == "1234"
            assert posted["pfc_confidence"] == "HIGH"
            assert posted["counterparties"] == '[{"name": "Synthetic merchant"}]'
            assert next(row for row in rows if row["pending"])["counterparties"] == "[]"
            assert not (await session.execute(select_transactions([]))).all()
    finally:
        await engine.dispose()


async def test_currency_preserves_unofficial_units_and_unknown_currency(db_url: str) -> None:
    engine, sessions = async_session_factory(db_url)
    try:
        async with sessions() as session:
            session.add(
                LinkRow(
                    item_id="currency-item",
                    products_requested=[],
                    products_authorized=[],
                    products_billed=[],
                    status="active",
                    access_token_secret="synthetic-token",
                )
            )
            await session.flush()
            session.add(
                AccountRow(
                    account_id="currency-account", item_id="currency-item", name="Synthetic", type="credit", raw_json={}
                )
            )
            await session.flush()
            session.add_all(
                [
                    TransactionRow(
                        transaction_id=identity,
                        account_id="currency-account",
                        item_id="currency-item",
                        date=date(2026, 1, 2),
                        amount=Decimal("0.01"),
                        name="Synthetic currency case",
                        pending=False,
                        removed=False,
                        iso_currency_code=iso,
                        raw_json={"unofficial_currency_code": unofficial},
                    )
                    for identity, iso, unofficial in [
                        ("iso", "USD", None),
                        ("unofficial", None, "BTC"),
                        ("unknown", None, None),
                        ("iso-first", "USD", "BTC"),
                    ]
                ]
            )
            await session.commit()
            rows = (await session.execute(select_transactions(["currency-account"]))).mappings().all()
            assert {row["transaction_id"]: row["currency"] for row in rows} == {
                "iso": "USD",
                "unofficial": "BTC",
                "unknown": None,
                "iso-first": "USD",
            }
    finally:
        await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
