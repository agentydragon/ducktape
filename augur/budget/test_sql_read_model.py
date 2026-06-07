"""Postgres-backed budget read-model behavior tests."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Generator
from datetime import date
from typing import Any

import pytest
import pytest_asyncio
import pytest_bazel
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from augur.budget import sql_read_model
from augur.budget.schema import (
    BucketDef,
    BucketKind,
    BudgetConfig,
    BudgetSourceConfig,
    MerchantSubstringRule,
    NameSubstringRule,
    Override,
    TransferDirection,
)
from augur.dates import DAYS_PER_MONTH
from plaid_utils.schema import AccountRow, LinkRow, TransactionRow, async_session_factory
from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.postgres import force_drop_database

_PLAID_MIGRATIONS_DIR = "_main/plaid_utils/migrations"


def _run_alembic_migrations(conn: Any) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(get_required_path(_PLAID_MIGRATIONS_DIR)))
    cfg.attributes["connection"] = conn
    alembic_command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _preload_postgres_images() -> None:
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres"


@pytest_asyncio.fixture
async def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> AsyncGenerator[str]:
    db_name = re.sub(r"[^a-z0-9]", "_", request.node.name.lower())[:45].rstrip("_") or "budget_sql_test"
    admin_engine = create_async_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin_engine.dispose()
    try:
        yield make_url(postgres_admin_url).set(database=db_name).render_as_string(hide_password=False)
    finally:
        await force_drop_database(postgres_admin_url, db_name)


@pytest_asyncio.fixture
async def session_factory(db_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine, factory = async_session_factory(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(_run_alembic_migrations)
    try:
        yield factory
    finally:
        await engine.dispose()


def _budget_config(overrides: tuple[Override, ...] = ()) -> BudgetConfig:
    return BudgetConfig(
        source=BudgetSourceConfig(plaid_account_ids=("checking",), coverage_starts=date(2026, 3, 15)),
        buckets=(
            BucketDef(id="custom", label="Custom", kind=BucketKind.EXPENSE, direction=TransferDirection.OUTFLOW),
            BucketDef(
                id="general_merchandise",
                label="General merchandise",
                kind=BucketKind.EXPENSE,
                direction=TransferDirection.OUTFLOW,
            ),
            BucketDef(id="groceries", label="Groceries", kind=BucketKind.EXPENSE, direction=TransferDirection.OUTFLOW),
            BucketDef(
                id="transfers_out", label="Transfers out", kind=BucketKind.TRANSFER, direction=TransferDirection.OUTFLOW
            ),
            BucketDef(
                id="transfers_in", label="Transfers in", kind=BucketKind.TRANSFER, direction=TransferDirection.INFLOW
            ),
            BucketDef(
                id="tax_refunds", label="Tax refunds", kind=BucketKind.TRANSFER, direction=TransferDirection.INFLOW
            ),
            BucketDef(id="income", label="Income", kind=BucketKind.INCOME, direction=TransferDirection.INFLOW),
            BucketDef(id="other", label="Other", kind=BucketKind.EXPENSE, direction=TransferDirection.OUTFLOW),
            BucketDef(id="other_in", label="Other in", kind=BucketKind.INFLOW, direction=TransferDirection.INFLOW),
        ),
        default_outflow_bucket_id="other",
        default_inflow_bucket_id="other_in",
        rules=(
            MerchantSubstringRule(pattern="Target", bucket_id="custom"),
            NameSubstringRule(pattern="Wealthfront", bucket_id="transfers_in"),
        ),
        overrides=overrides,
        include_default_rules=True,
        lumpy_threshold_usd=500.0,
    )


def _tx(
    transaction_id: str,
    *,
    account_id: str = "checking",
    item_id: str = "item-budget",
    on: date,
    amount: float,
    name: str,
    merchant_name: str | None = None,
    pfc_primary: str | None = None,
    pfc_detailed: str | None = None,
    pending: bool = False,
    removed: bool = False,
) -> TransactionRow:
    return TransactionRow(
        transaction_id=transaction_id,
        account_id=account_id,
        item_id=item_id,
        date=on,
        amount=amount,
        iso_currency_code="USD",
        name=name,
        merchant_name=merchant_name,
        pending=pending,
        pending_transaction_id=None,
        pfc_primary=pfc_primary,
        pfc_detailed=pfc_detailed,
        removed=removed,
        raw_json={},
    )


async def _seed_budget_rows(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                LinkRow(
                    item_id="item-budget",
                    institution_id="ins_budget",
                    institution_name="Budget Bank",
                    label=None,
                    link_profile="cashflow",
                    products_requested=["transactions"],
                    products_authorized=["transactions"],
                    products_billed=[],
                    status="active",
                    access_token_secret="budget-token",
                ),
                LinkRow(
                    item_id="item-revoked",
                    institution_id="ins_revoked",
                    institution_name="Revoked Bank",
                    label=None,
                    link_profile="cashflow",
                    products_requested=["transactions"],
                    products_authorized=["transactions"],
                    products_billed=[],
                    status="revoked",
                    access_token_secret="revoked-token",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                AccountRow(
                    account_id="checking",
                    item_id="item-budget",
                    name="Checking",
                    official_name=None,
                    mask=None,
                    type="depository",
                    subtype="checking",
                    iso_currency_code="USD",
                    raw_json={},
                ),
                AccountRow(
                    account_id="other-account",
                    item_id="item-budget",
                    name="Other account",
                    official_name=None,
                    mask=None,
                    type="depository",
                    subtype="checking",
                    iso_currency_code="USD",
                    raw_json={},
                ),
                AccountRow(
                    account_id="revoked-account",
                    item_id="item-revoked",
                    name="Revoked account",
                    official_name=None,
                    mask=None,
                    type="depository",
                    subtype="checking",
                    iso_currency_code="USD",
                    raw_json={},
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                _tx(
                    "target_preempt",
                    on=date(2026, 3, 20),
                    amount=40.0,
                    name="TARGET STORE",
                    merchant_name="Target",
                    pfc_primary="GENERAL_MERCHANDISE",
                ),
                _tx(
                    "anthropic_default_skipped",
                    on=date(2026, 3, 21),
                    amount=20.0,
                    name="ANTHROPIC",
                    merchant_name="Anthropic",
                ),
                _tx(
                    "wealthfront_out",
                    on=date(2026, 3, 22),
                    amount=30000.0,
                    name="Wealthfront",
                    merchant_name=None,
                    pfc_primary="TRANSFER_OUT",
                ),
                _tx(
                    "wealthfront_in",
                    on=date(2026, 3, 23),
                    amount=-12000.0,
                    name="Wealthfront",
                    merchant_name=None,
                    pfc_primary="INCOME",
                    pfc_detailed="INCOME_OTHER_INCOME",
                ),
                _tx(
                    "tax_refund",
                    on=date(2026, 3, 24),
                    amount=-1246.0,
                    name="FRANCHISE TAX BD DES:CASTTAXRFD",
                    merchant_name=None,
                    pfc_primary="INCOME",
                    pfc_detailed="INCOME_TAX_REFUND",
                ),
                _tx("zero", on=date(2026, 4, 1), amount=0.0, name="ZERO"),
                _tx(
                    "grocery_lumpy",
                    on=date(2026, 4, 5),
                    amount=1000.0,
                    name="GROCERY",
                    merchant_name="Grocery",
                    pfc_primary="FOOD_AND_DRINK",
                    pfc_detailed="FOOD_AND_DRINK_GROCERIES",
                ),
                _tx(
                    "payroll",
                    on=date(2026, 4, 6),
                    amount=-5000.0,
                    name="PAYROLL",
                    pfc_primary="INCOME",
                    pfc_detailed="INCOME_WAGES",
                ),
                _tx(
                    "other_account_excluded",
                    account_id="other-account",
                    on=date(2026, 4, 7),
                    amount=999.0,
                    name="OTHER ACCOUNT",
                    merchant_name="Target",
                ),
                _tx("pending_excluded", on=date(2026, 4, 8), amount=25.0, name="PENDING", pending=True),
                _tx("removed_excluded", on=date(2026, 4, 9), amount=25.0, name="REMOVED", removed=True),
                _tx(
                    "revoked_excluded",
                    account_id="revoked-account",
                    item_id="item-revoked",
                    on=date(2026, 4, 10),
                    amount=25.0,
                    name="REVOKED",
                ),
                _tx("outside_window", on=date(2026, 5, 1), amount=25.0, name="OUTSIDE"),
            ]
        )
        await session.commit()


def _monthly_by_bucket(response) -> dict[str, tuple[float, ...]]:
    return {row.bucket_id: row.monthly_amounts for row in response.monthly_by_bucket}


def _counts_by_bucket(response) -> dict[str, int]:
    return {row.bucket_id: row.transaction_count for row in response.monthly_by_bucket}


def _averages_by_bucket(response) -> dict[str, float]:
    return {row.bucket_id: row.window_monthly_avg for row in response.monthly_by_bucket}


async def test_sql_budget_snapshot_preserves_classification_and_aggregation_semantics(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_budget_rows(session_factory)
    config = _budget_config()

    response = await sql_read_model.read_budget_snapshot(
        session_factory=session_factory,
        config=config,
        window_start=date(2026, 3, 15),
        window_end=date(2026, 4, 30),
        account_ids=("checking",),
    )

    assert response.months == (date(2026, 3, 1), date(2026, 4, 1))
    assert _monthly_by_bucket(response) == {
        "custom": (40.0, 0.0),
        "general_merchandise": (0.0, 0.0),
        "groceries": (0.0, 1000.0),
        "transfers_out": (30000.0, 0.0),
        "transfers_in": (-12000.0, 0.0),
        "tax_refunds": (-1246.0, 0.0),
        "income": (0.0, -5000.0),
        "other": (20.0, 0.0),
        "other_in": (0.0, 0.0),
    }
    assert _counts_by_bucket(response) == {
        "custom": 1,
        "general_merchandise": 0,
        "groceries": 1,
        "transfers_out": 1,
        "transfers_in": 1,
        "tax_refunds": 1,
        "income": 1,
        "other": 2,
        "other_in": 0,
    }
    days = (date(2026, 4, 30) - date(2026, 3, 15)).days + 1
    assert _averages_by_bucket(response)["groceries"] == pytest.approx(1000.0 / days * DAYS_PER_MONTH)
    assert _averages_by_bucket(response)["income"] == pytest.approx(-5000.0 / days * DAYS_PER_MONTH)
    assert [row.transaction_id for row in response.lumpy] == ["grocery_lumpy"]
    assert response.coverage_starts == date(2026, 3, 15)


async def test_sql_budget_drilldown_filters_in_sql_and_enriches_account_labels(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_budget_rows(session_factory)
    config = _budget_config()

    response = await sql_read_model.read_budget_bucket_transactions(
        session_factory=session_factory,
        config=config,
        bucket_id="transfers_in",
        window_start=date(2026, 3, 15),
        window_end=date(2026, 4, 30),
        account_ids=("checking",),
    )

    assert response.bucket_id == "transfers_in"
    assert [row.transaction_id for row in response.transactions] == ["wealthfront_in"]
    row = response.transactions[0]
    assert row.amount == -12000.0
    assert row.account_name == "Checking"
    assert row.institution_name == "Budget Bank"
    assert row.bucket_id == "transfers_in"


async def test_sql_budget_drilldown_rejects_unknown_bucket(session_factory: async_sessionmaker[AsyncSession]) -> None:
    config = _budget_config()

    with pytest.raises(ValueError, match="unknown bucket_id"):
        await sql_read_model.read_budget_bucket_transactions(
            session_factory=session_factory,
            config=config,
            bucket_id="missing",
            window_start=date(2026, 3, 15),
            window_end=date(2026, 4, 30),
            account_ids=("checking",),
        )


async def test_sql_budget_overrides_beat_rules_and_ignore_direction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_budget_rows(session_factory)
    config = _budget_config(
        overrides=(
            # Pre-empts the `Target -> custom` rule.
            Override(transaction_id="target_preempt", bucket_id="groceries", note="2026-03-20 $40 TARGET STORE"),
            # Ungated: an inflow-signed (-12000) txn forced into an outflow expense bucket --
            # something the direction-gated rules could never do.
            Override(transaction_id="wealthfront_in", bucket_id="custom", note="2026-03-23 -$12000 Wealthfront"),
        )
    )

    response = await sql_read_model.read_budget_snapshot(
        session_factory=session_factory,
        config=config,
        window_start=date(2026, 3, 15),
        window_end=date(2026, 4, 30),
        account_ids=("checking",),
    )

    monthly = _monthly_by_bucket(response)
    assert monthly["groceries"] == (40.0, 1000.0)  # target_preempt (Mar) joins grocery_lumpy (Apr)
    assert monthly["custom"] == (-12000.0, 0.0)  # wealthfront_in forced here despite its inflow sign
    assert monthly["transfers_in"] == (0.0, 0.0)  # wealthfront_in no longer lands here
    assert response.stale_overrides == ()


async def test_sql_budget_snapshot_reports_stale_overrides(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed_budget_rows(session_factory)
    config = _budget_config(
        overrides=(
            Override(transaction_id="target_preempt", bucket_id="groceries", note="live row"),
            Override(transaction_id="removed_excluded", bucket_id="groceries", note="points at a removed row"),
            Override(transaction_id="ghost_txn_id", bucket_id="groceries", note="no such txn (e.g. post-relink)"),
        )
    )

    response = await sql_read_model.read_budget_snapshot(
        session_factory=session_factory,
        config=config,
        window_start=date(2026, 3, 15),
        window_end=date(2026, 4, 30),
        account_ids=("checking",),
    )

    # Global existence probe: the live row is fine; the removed row and the absent id are stale.
    assert response.stale_overrides == ("ghost_txn_id", "removed_excluded")


if __name__ == "__main__":
    pytest_bazel.main()
