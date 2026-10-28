"""Upgrade real legacy data and reject unverifiable capture provenance atomically."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_bazel
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from finance.plaid.db.link_store import PlaidLinkStorage
from util.db_url import render_postgresql_url
from util.testing.postgres import DatabaseNames, create_temporary_database

_MIGRATIONS = Path(__file__).parent / "migrations"
_CAPTURED = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.attributes["connection"] = connection
    return config


@contextmanager
def _connection(db_url: str) -> Iterator[Connection]:
    with ExitStack() as resources:
        engine = create_engine(db_url)
        resources.callback(engine.dispose)
        yield resources.enter_context(engine.connect())


def _seed_account(connection: Connection) -> None:
    connection.execute(
        text(
            """INSERT INTO links(item_id,products_requested,products_authorized,products_billed,status,
               access_token_secret,created_at,updated_at)
               VALUES ('fake-item','["investments"]','["investments"]','[]','active','fake-secret',:at,:at)"""
        ),
        {"at": _CAPTURED},
    )
    connection.execute(
        text(
            """INSERT INTO accounts(account_id,item_id,name,type,raw_json,updated_at)
               VALUES ('fake-account','fake-item','Fake Brokerage','investment','{}',:at)"""
        ),
        {"at": _CAPTURED},
    )
    connection.execute(
        text(
            """INSERT INTO securities(security_id,name,raw_json,updated_at)
               VALUES ('fake-security','Fake Security','{}',:at)"""
        ),
        {"at": _CAPTURED},
    )
    connection.execute(
        text(
            """INSERT INTO balance_snapshots(account_id,item_id,captured_at,current,iso_currency_code)
               VALUES ('fake-account','fake-item',:at,123.45,'USD')"""
        ),
        {"at": _CAPTURED},
    )
    connection.execute(
        text(
            """INSERT INTO transactions(transaction_id,account_id,item_id,date,amount,name,pending,
               raw_json,updated_at) VALUES ('fake-transaction','fake-account','fake-item','2026-10-01',
               0.1,'Fake transaction',false,CAST(:raw AS jsonb),:at)"""
        ),
        {"at": _CAPTURED, "raw": json.dumps({"amount": 0.1})},
    )


def _seed_capture(
    connection: Connection,
    *,
    at: datetime = _CAPTURED,
    empty: bool = False,
    status: str = "succeeded",
    event_count: int = 1,
    removed_item: bool = False,
) -> None:
    item_id = "fake-removed-item" if removed_item else "fake-item"
    account_id = "fake-removed-account" if removed_item else "fake-account"
    run_id = uuid4()
    holding = {
        "account_id": account_id,
        "security_id": "fake-security",
        "quantity": 0.1,
        "institution_value": 12.34,
        "iso_currency_code": "USD",
    }
    payload = {
        "holdings": [] if empty else [holding],
        "securities": [{"security_id": "fake-security", "name": "Fake Security"}],
        "accounts": [
            {
                "account_id": account_id,
                "name": "Fake Brokerage",
                "type": "investment",
                "balances": {"current": 0 if empty else 12.34, "iso_currency_code": "USD"},
            }
        ],
    }
    connection.execute(
        text(
            """INSERT INTO sync_runs(run_id,trigger,mode,item_id,configured_windows,status,started_at,finished_at)
               VALUES (:run,'test','v1_cursor_transactions',:item,'{}',:status,:started,:finished)"""
        ),
        {
            "run": run_id,
            "item": item_id,
            "status": status,
            "started": at - timedelta(seconds=1),
            "finished": at + timedelta(seconds=10),
        },
    )
    for _ in range(event_count):
        connection.execute(
            text(
                """INSERT INTO plaid_api_events(sync_run_id,endpoint,item_id,status,request_json,response_json,created_at)
                   VALUES (:run,'investments/holdings/get',:item,'ok','{}',CAST(:payload AS jsonb),:at)"""
            ),
            {"run": run_id, "item": item_id, "payload": json.dumps(payload), "at": at + timedelta(seconds=5)},
        )
    if not empty and not removed_item:
        connection.execute(
            text(
                """INSERT INTO holding_snapshots(account_id,security_id,item_id,captured_at,quantity,
                   institution_value,iso_currency_code,raw_json)
                   VALUES (:account,'fake-security',:item,:at,0.1,12.34,'USD',CAST(:holding AS jsonb))"""
            ),
            {"account": account_id, "item": item_id, "at": at, "holding": json.dumps(holding)},
        )


@pytest.mark.parametrize("existing_public_grant", [False, True])
def test_upgrade_recovers_exact_captures_and_coverage(
    postgres_admin_url: str, database_names: DatabaseNames, existing_public_grant: bool
) -> None:
    view_acl = text(
        """SELECT jsonb_agg(jsonb_build_array(a.grantee,a.privilege_type,a.is_grantable)
           ORDER BY a.grantee,a.privilege_type,a.is_grantable)
           FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
           WHERE c.oid='current_transactions'::regclass"""
    )
    with create_temporary_database(postgres_admin_url, database_names()) as db_url, _connection(db_url) as connection:
        with connection.begin():
            command.upgrade(_config(connection), "0009")
            _seed_account(connection)
            _seed_capture(connection, status="failed")
            _seed_capture(connection, at=_CAPTURED + timedelta(days=1), empty=True)
            _seed_capture(connection, removed_item=True)
            if existing_public_grant:
                connection.execute(text("GRANT SELECT ON current_transactions TO PUBLIC"))
            original_acl = connection.scalar(view_acl)
            # Recreating the view must not accidentally acquire newer default grants.
            connection.execute(text("ALTER DEFAULT PRIVILEGES GRANT INSERT ON TABLES TO PUBLIC"))
        with connection.begin():
            command.upgrade(_config(connection), "0010")
            revision = ScriptDirectory.from_config(_config(connection)).get_revision("0010")
            assert revision is not None
            revision.module.backfill(connection)
        with connection.begin():
            captures = connection.execute(
                text("SELECT item_id,captured_at FROM holding_captures ORDER BY captured_at")
            ).all()
            assert [tuple(row) for row in captures] == [
                ("fake-item", _CAPTURED),
                ("fake-item", _CAPTURED + timedelta(days=1, seconds=5)),
            ]
            rows = connection.execute(
                text(
                    """SELECT a.account_id,a.current,a.iso_currency_code FROM holding_capture_accounts a
                       JOIN holding_captures c ON c.id=a.capture_id ORDER BY c.captured_at"""
                )
            ).all()
            assert [tuple(row) for row in rows] == [
                ("fake-account", Decimal("12.34"), "USD"),
                ("fake-account", Decimal(0), "USD"),
            ]
            holding = connection.execute(
                text("SELECT captured_at,quantity,institution_value,capture_id IS NOT NULL FROM holding_snapshots")
            ).one()
            assert tuple(holding) == (_CAPTURED, Decimal("0.1"), Decimal("12.34"), True)
            assert connection.scalar(text("SELECT amount FROM transactions")) == Decimal("0.1")
            assert connection.scalar(text("SELECT amount FROM current_transactions")) == Decimal("0.1")
            assert connection.scalar(view_acl) == original_acl
            assert connection.scalar(text("SELECT current FROM balance_snapshots")) == Decimal("123.45")
            assert connection.scalar(text("SELECT count(*) FROM plaid_api_events")) == 3


@pytest.mark.parametrize("event_count", [0, 2], ids=["missing-response", "ambiguous-response"])
def test_upgrade_rolls_back_unverifiable_history(
    postgres_admin_url: str, database_names: DatabaseNames, event_count: int
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as db_url, _connection(db_url) as connection:
        with connection.begin():
            command.upgrade(_config(connection), "0009")
            _seed_account(connection)
            _seed_capture(connection, event_count=event_count)
        with pytest.raises(DBAPIError), connection.begin():
            command.upgrade(_config(connection), "0010")
        with connection.begin():
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
            assert connection.scalar(text("SELECT to_regclass('holding_captures')")) is None
            assert connection.scalar(text("SELECT count(*) FROM holding_snapshots")) == 1
            assert (
                connection.scalar(
                    text(
                        "SELECT data_type FROM information_schema.columns WHERE table_name='transactions' AND column_name='amount'"
                    )
                )
                == "double precision"
            )


def test_failed_empty_receipt_is_not_an_applied_capture(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with (
        create_temporary_database(postgres_admin_url, database_names()) as db_url,
        _connection(db_url) as connection,
        connection.begin(),
    ):
        command.upgrade(_config(connection), "0009")
        _seed_account(connection)
        _seed_capture(connection, empty=True, status="failed")
        command.upgrade(_config(connection), "0010")
        assert connection.scalar(text("SELECT count(*) FROM holding_captures")) == 0
        assert connection.scalar(text("SELECT count(*) FROM plaid_api_events")) == 1


async def test_concurrent_initialization_serializes_migrations(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as db_url:
        async_url = render_postgresql_url(db_url, "postgresql+asyncpg")
        stores = await asyncio.gather(PlaidLinkStorage.initialize(async_url), PlaidLinkStorage.initialize(async_url))
        try:
            with _connection(db_url) as connection:
                assert connection.scalar(text("SELECT count(*) FROM holding_captures")) == 0
        finally:
            await asyncio.gather(*(store.close() for store in stores))


if __name__ == "__main__":
    pytest_bazel.main()
