"""Behavioral coverage for the shared portfolio snapshot selectors."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest_bazel
from sqlalchemy import insert
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine

from finance.plaid.db.models import (
    AccountRow,
    BalanceSnapshotRow,
    HoldingCaptureAccountRow,
    HoldingCaptureRow,
    HoldingSnapshotRow,
    LinkRow,
    PlaidApiEventRow,
    SecurityRow,
)
from finance.plaid.spend.portfolio_queries import select_account_balances, select_holding_batches


def _capture(
    connection: Connection, *, item_id: str, captured_at: datetime, accounts: dict[str, tuple[Decimal, str]]
) -> int:
    api_event_id = connection.execute(
        insert(PlaidApiEventRow)
        .values(
            endpoint="investments/holdings/get",
            item_id=item_id,
            status="ok",
            request_json={},
            response_json={},
            created_at=captured_at,
        )
        .returning(PlaidApiEventRow.id)
    ).scalar_one()
    capture_id = connection.execute(
        insert(HoldingCaptureRow)
        .values(item_id=item_id, captured_at=captured_at, api_event_id=api_event_id)
        .returning(HoldingCaptureRow.id)
    ).scalar_one()
    if accounts:
        connection.execute(
            insert(HoldingCaptureAccountRow),
            [
                {
                    "capture_id": capture_id,
                    "account_id": account_id,
                    "item_id": item_id,
                    "current": current,
                    "iso_currency_code": currency,
                }
                for account_id, (current, currency) in accounts.items()
            ],
        )
    return capture_id


def test_selectors_use_latest_item_capture_coverage_and_balances(postgres_engine: Engine) -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    earlier = now - timedelta(hours=1)
    with postgres_engine.begin() as connection:
        connection.execute(
            insert(LinkRow),
            [
                {
                    "item_id": item,
                    "institution_name": "Synthetic institution",
                    "products_requested": [],
                    "products_authorized": [],
                    "products_billed": [],
                    "status": status,
                    "access_token_secret": "fake-secret-reference",
                    "created_at": now,
                    "updated_at": now,
                }
                for item, status in [("fake-active", "active"), ("fake-inactive", "inactive"), ("fake-empty", "active")]
            ],
        )
        connection.execute(
            insert(AccountRow),
            [
                {
                    "account_id": account,
                    "item_id": item,
                    "name": "Synthetic account",
                    "type": "investment",
                    "iso_currency_code": "USD",
                    "raw_json": {},
                    "detached_at": detached,
                    "updated_at": now,
                }
                for account, item, detached in [
                    ("fake-selected", "fake-active", None),
                    ("fake-no-coverage", "fake-active", None),
                    ("fake-unselected", "fake-active", None),
                    ("fake-detached", "fake-active", now),
                    ("fake-inactive", "fake-inactive", None),
                    ("fake-empty", "fake-empty", None),
                ]
            ],
        )
        connection.execute(
            insert(BalanceSnapshotRow),
            [
                {
                    "account_id": account,
                    "item_id": item,
                    "captured_at": captured,
                    "current": amount,
                    "iso_currency_code": "USD",
                }
                for account, item in [
                    ("fake-selected", "fake-active"),
                    ("fake-unselected", "fake-active"),
                    ("fake-detached", "fake-active"),
                    ("fake-inactive", "fake-inactive"),
                ]
                for captured, amount in [(earlier, 10), (now, 20), (now, 30)]
            ],
        )
        connection.execute(
            insert(SecurityRow),
            [
                {
                    "security_id": security,
                    "name": security,
                    "ticker_symbol": ticker,
                    "type": "equity",
                    "raw_json": {},
                    "updated_at": now,
                }
                for security, ticker in [
                    ("fake-sold", "SOLD"),
                    ("fake-held", "HELD"),
                    ("fake-uncovered", "UNCOVERED"),
                    ("fake-sold-empty", "SOLD-EMPTY"),
                ]
            ],
        )

        earlier_capture_id = _capture(
            connection,
            item_id="fake-active",
            captured_at=earlier,
            accounts={"fake-selected": (Decimal(700), "USD"), "fake-no-coverage": (Decimal(50), "USD")},
        )
        latest_capture_id = _capture(
            connection, item_id="fake-active", captured_at=now, accounts={"fake-selected": (Decimal(25), "USD")}
        )
        empty_previous_capture_id = _capture(
            connection, item_id="fake-empty", captured_at=earlier, accounts={"fake-empty": (Decimal(10), "USD")}
        )
        empty_capture_id = _capture(
            connection, item_id="fake-empty", captured_at=now, accounts={"fake-empty": (Decimal(0), "USD")}
        )
        connection.execute(
            insert(HoldingSnapshotRow),
            [
                {
                    "account_id": account,
                    "item_id": item,
                    "security_id": security,
                    "capture_id": capture_id,
                    "captured_at": captured,
                    "institution_value": value,
                    "iso_currency_code": "USD",
                    "raw_json": {},
                }
                for account, item, security, capture_id, captured, value in [
                    ("fake-selected", "fake-active", "fake-sold", earlier_capture_id, earlier, 30),
                    ("fake-selected", "fake-active", "fake-held", earlier_capture_id, earlier, 30),
                    ("fake-no-coverage", "fake-active", "fake-uncovered", earlier_capture_id, earlier, 50),
                    # The capture header supplies observation time even if a legacy row timestamp drifts.
                    ("fake-selected", "fake-active", "fake-held", latest_capture_id, earlier, 25),
                    ("fake-empty", "fake-empty", "fake-sold-empty", empty_previous_capture_id, earlier, 10),
                ]
            ],
        )

        selected = ["fake-selected", "fake-no-coverage", "fake-detached", "fake-inactive"]
        balances = connection.execute(select_account_balances(selected)).mappings().all()
        holdings = connection.execute(select_holding_batches(selected)).mappings().all()
        capture_balances = (
            connection.execute(
                select_account_balances(selected, holding_account_ids=["fake-selected", "fake-no-coverage"])
            )
            .mappings()
            .all()
        )
        empty_balances = (
            connection.execute(select_account_balances(["fake-empty"], holding_account_ids=["fake-empty"]))
            .mappings()
            .all()
        )

        assert {row["account_id"]: row["current"] for row in balances} == {
            "fake-selected": 30,
            "fake-no-coverage": None,
        }
        assert [(row["account_id"], row["security_id"], row["ticker"]) for row in holdings] == [
            ("fake-selected", "fake-held", "HELD")
        ]
        assert holdings[0]["captured_at"] == now
        capture_rows = {row["account_id"]: row for row in capture_balances}
        assert capture_rows["fake-selected"]["current"] == Decimal(25)
        assert capture_rows["fake-selected"]["holding_capture_id"] == latest_capture_id
        assert capture_rows["fake-selected"]["holding_capture_covered"] is True
        assert capture_rows["fake-no-coverage"]["holding_capture_id"] == latest_capture_id
        assert capture_rows["fake-no-coverage"]["holding_capture_covered"] is False
        assert empty_balances[0]["current"] == Decimal(0)
        assert empty_balances[0]["holding_capture_id"] == empty_capture_id
        assert empty_balances[0]["holding_capture_covered"] is True
        assert connection.execute(select_holding_batches(["fake-empty"])).all() == []
        assert connection.execute(select_holding_batches(["fake-no-coverage"])).all() == []
        assert connection.execute(select_account_balances([])).all() == []
        assert connection.execute(select_holding_batches([])).all() == []


def test_holding_balance_selector_flags_unlinked_rows(postgres_engine: Engine) -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    with postgres_engine.begin() as connection:
        connection.execute(
            insert(LinkRow),
            {
                "item_id": "fake-active",
                "institution_name": "Synthetic institution",
                "products_requested": [],
                "products_authorized": [],
                "products_billed": [],
                "status": "active",
                "access_token_secret": "fake-secret-reference",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            insert(AccountRow),
            {
                "account_id": "fake-selected",
                "item_id": "fake-active",
                "name": "Synthetic account",
                "type": "investment",
                "iso_currency_code": "USD",
                "raw_json": {},
                "updated_at": now,
            },
        )
        connection.execute(
            insert(SecurityRow),
            {
                "security_id": "fake-held",
                "name": "Synthetic security",
                "ticker_symbol": "HELD",
                "type": "equity",
                "raw_json": {},
                "updated_at": now,
            },
        )
        capture_id = _capture(
            connection, item_id="fake-active", captured_at=now, accounts={"fake-selected": (Decimal(25), "USD")}
        )
        connection.execute(
            insert(HoldingSnapshotRow),
            {
                "account_id": "fake-selected",
                "item_id": "fake-active",
                "security_id": "fake-held",
                "capture_id": None,
                "captured_at": now,
                "institution_value": 25,
                "iso_currency_code": "USD",
                "raw_json": {},
            },
        )

        row = (
            connection.execute(select_account_balances(["fake-selected"], holding_account_ids=["fake-selected"]))
            .mappings()
            .one()
        )
        assert row["holding_capture_id"] == capture_id
        assert row["holding_capture_has_unapplied_observations"] is True


def test_holding_balance_selector_flags_empty_legacy_receipt_until_a_new_capture(postgres_engine: Engine) -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    with postgres_engine.begin() as connection:
        connection.execute(
            insert(LinkRow),
            {
                "item_id": "fake-active",
                "institution_name": "Synthetic institution",
                "products_requested": [],
                "products_authorized": [],
                "products_billed": [],
                "status": "active",
                "access_token_secret": "fake-secret-reference",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            insert(AccountRow),
            {
                "account_id": "fake-selected",
                "item_id": "fake-active",
                "name": "Synthetic account",
                "type": "investment",
                "iso_currency_code": "USD",
                "raw_json": {},
                "updated_at": now,
            },
        )
        _capture(connection, item_id="fake-active", captured_at=now, accounts={"fake-selected": (Decimal(25), "USD")})
        connection.execute(
            insert(PlaidApiEventRow),
            {
                "endpoint": "investments/holdings/get",
                "item_id": "fake-active",
                "status": "ok",
                "request_json": {},
                "response_json": {},
                "created_at": now + timedelta(minutes=1),
            },
        )
        stale = (
            connection.execute(select_account_balances(["fake-selected"], holding_account_ids=["fake-selected"]))
            .mappings()
            .one()
        )
        assert stale["holding_capture_has_unapplied_observations"] is True

        _capture(
            connection,
            item_id="fake-active",
            captured_at=now + timedelta(minutes=2),
            accounts={"fake-selected": (Decimal(25), "USD")},
        )
        fresh = (
            connection.execute(select_account_balances(["fake-selected"], holding_account_ids=["fake-selected"]))
            .mappings()
            .one()
        )
        assert fresh["holding_capture_has_unapplied_observations"] is False


def test_selectors_bind_hostile_values() -> None:
    selector = "fake-account'); DROP TABLE accounts; --"
    for statement in [select_account_balances([selector]), select_holding_batches([selector])]:
        compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True})
        assert selector not in str(compiled)
        assert selector in compiled.params.values()


if __name__ == "__main__":
    pytest_bazel.main()
