"""Verify Plaid migrations serialize and enforce capture integrity."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_bazel
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from finance.plaid.db.link_store import _MIGRATIONS_DIR, PlaidLinkStorage
from finance.plaid.db.models import (
    AccountRow,
    HoldingCaptureAccountRow,
    HoldingCaptureRow,
    HoldingSnapshotRow,
    LinkRow,
    PlaidApiEventRow,
    SecurityRow,
)
from util.db_url import render_postgresql_url
from util.testing.postgres import DatabaseNames, create_temporary_database


@contextmanager
def _connection(db_url: str) -> Iterator[Connection]:
    with ExitStack() as resources:
        engine = create_engine(db_url)
        resources.callback(engine.dispose)
        yield resources.enter_context(engine.begin())


def _upgrade_to_revision(db_url: str, revision: str) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    engine = create_engine(db_url)
    try:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            alembic_command.upgrade(cfg, revision)
    finally:
        engine.dispose()


def _insert_holding_snapshot(connection: Connection, *, capture_linked: bool) -> int:
    item_id = "migration-item"
    account_id = "migration-account"
    security_id = "migration-security"
    captured_at = datetime(2026, 11, 1, tzinfo=UTC)
    connection.execute(
        insert(LinkRow).values(
            item_id=item_id,
            products_requested=["investments"],
            products_authorized=["investments"],
            products_billed=[],
            status="active",
            access_token_secret="synthetic-secret-reference",
            created_at=captured_at,
            updated_at=captured_at,
        )
    )
    connection.execute(
        insert(AccountRow).values(
            account_id=account_id,
            item_id=item_id,
            name="Synthetic brokerage",
            type="investment",
            raw_json={},
            updated_at=captured_at,
        )
    )
    connection.execute(
        insert(SecurityRow).values(
            security_id=security_id, raw_json={"security_id": security_id}, updated_at=captured_at
        )
    )
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
    connection.execute(
        insert(HoldingCaptureAccountRow).values(
            capture_id=capture_id,
            account_id=account_id,
            item_id=item_id,
            current=Decimal("100.00"),
            iso_currency_code="USD",
        )
    )
    return connection.execute(
        insert(HoldingSnapshotRow)
        .values(
            account_id=account_id,
            item_id=item_id,
            security_id=security_id,
            capture_id=capture_id if capture_linked else None,
            captured_at=captured_at,
            quantity=Decimal("1.25"),
            cost_basis=Decimal("10.01"),
            institution_price=Decimal("9.87"),
            institution_value=Decimal("12.34"),
            iso_currency_code="USD",
            raw_json={"holding": "preserve"},
        )
        .returning(HoldingSnapshotRow.id)
    ).scalar_one()


async def test_concurrent_initialization_serializes_migrations(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as db_url:
        async_url = render_postgresql_url(db_url, "postgresql+asyncpg")
        stores = await asyncio.gather(PlaidLinkStorage.initialize(async_url), PlaidLinkStorage.initialize(async_url))
        try:
            with _connection(db_url) as connection:
                assert connection.scalar(text("SELECT count(*) FROM holding_captures")) == 0
                assert (
                    connection.scalar(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_name='holding_snapshots' AND column_name='capture_id'"
                        )
                    )
                    == "NO"
                )
        finally:
            await asyncio.gather(*(store.close() for store in stores))


async def test_required_capture_migration_preserves_existing_snapshot(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as db_url:
        _upgrade_to_revision(db_url, "0010")
        with _connection(db_url) as connection:
            snapshot_id = _insert_holding_snapshot(connection, capture_linked=True)
            before = dict(
                connection.execute(select(HoldingSnapshotRow.__table__).where(HoldingSnapshotRow.id == snapshot_id))
                .mappings()
                .one()
            )

        store = await PlaidLinkStorage.initialize(render_postgresql_url(db_url, "postgresql+asyncpg"))
        await store.close()

        with _connection(db_url) as connection:
            after = dict(
                connection.execute(select(HoldingSnapshotRow.__table__).where(HoldingSnapshotRow.id == snapshot_id))
                .mappings()
                .one()
            )
            assert after == before
            assert (
                connection.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name='holding_snapshots' AND column_name='capture_id'"
                    )
                )
                == "NO"
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text("UPDATE holding_snapshots SET capture_id = NULL WHERE id = :snapshot_id"),
                    {"snapshot_id": snapshot_id},
                )


async def test_required_capture_migration_refuses_unassigned_snapshot(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as db_url:
        _upgrade_to_revision(db_url, "0010")
        with _connection(db_url) as connection:
            snapshot_id = _insert_holding_snapshot(connection, capture_linked=False)

        async_url = render_postgresql_url(db_url, "postgresql+asyncpg")
        with pytest.raises(IntegrityError, match="capture_id"):
            await PlaidLinkStorage.initialize(async_url)

        with _connection(db_url) as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0010"
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM holding_snapshots WHERE id = :snapshot_id AND capture_id IS NULL"),
                    {"snapshot_id": snapshot_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name='holding_snapshots' AND column_name='capture_id'"
                    )
                )
                == "YES"
            )


if __name__ == "__main__":
    pytest_bazel.main()
