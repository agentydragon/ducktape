"""Real-Postgres round trip for the migration chain: every downgrade runs, what a migration promises
to carry forward survives it, and head lands exactly on the ORM metadata."""

from pathlib import Path
from uuid import UUID

import pytest_bazel
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.db import Base

NOTE = "Existing deployed note — preserved."


def _seed_at_0005(connection: Connection) -> None:
    """A decided request in the 0005-era shape, written out rather than produced through the ORM: a
    row seeded at head would be whatever head happens to look like, and would not survive a migration
    that drops rows (`0016`) on the way down to where the ascent has to start. Revisions below 0005
    are frozen, so this is too; the historical column spellings exist only to be migrated off."""
    # A bound id has to arrive typed: a str binds as VARCHAR, which Postgres will not compare to uuid.
    request = UUID("00000000-0000-4000-8000-000000000001")
    connection.execute(
        text(
            """
            INSERT INTO action_request
                (id, idempotency_key, action, arguments, origin, correlation,
                 caller_principal, state, version, created_at, updated_at)
            VALUES
                (:request, 'migration-request', '{"group": "test", "name": "echo"}', '{}', '{}', '{}',
                 'service-account:agentplane-test:test-caller', 'denied', 2, now(), now())
            """
        ).bindparams(request=request)
    )
    connection.execute(
        text(
            "INSERT INTO action_event (request_id, sequence, state, at) VALUES (:request, 1, 'denied', now())"
        ).bindparams(request=request)
    )
    connection.execute(
        text(
            """
            INSERT INTO action_decision
                (id, request_id, verdict, provider, issuer, private_reason, idempotency_key, decided_at)
            VALUES
                ('00000000-0000-4000-8000-000000000002', :request, 'deny', 'human_operator',
                 'test:operator', :note, 'migration-decision', now())
            """
        ).bindparams(request=request, note=NOTE)
    )


def _round_trip(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["connection"] = connection
    config.attributes["target_metadata"] = Base.metadata
    # 0005 is the floor: its downgrade raises, there being no legacy Action identity to restore.
    command.downgrade(config, "0005_structured_action")
    _seed_at_0005(connection)

    # Up to the last migration that carries rows forward; `0016` deliberately does not.
    command.upgrade(config, "0015_action_request_context")
    assert connection.scalar(text("SELECT decision_note FROM action_decision")) == NOTE
    # Columns added beside an existing row arrive empty rather than backfilled with a stand-in...
    assert connection.scalar(text("SELECT count(*) FROM action_event WHERE actor_principal IS NOT NULL")) == 0
    assert connection.scalar(text("SELECT count(*) FROM action_request WHERE external_grant IS NOT NULL")) == 0
    assert connection.scalar(text("SELECT count(*) FROM action_request WHERE description IS NOT NULL")) == 0
    # ...except a title, which the approval surfaces render: a request submitted before callers could
    # supply one must come back with something, not a blank line forever.
    assert connection.scalar(text("SELECT title FROM action_request"))

    # `0016` stores a principal as its own fields and drops what the old encoding held rather than
    # parsing it apart, in both directions. That is the documented behaviour.
    command.upgrade(config, "head")
    assert connection.scalar(text("SELECT count(*) FROM action_request")) == 0
    assert connection.scalar(text("SELECT count(*) FROM action_decision")) == 0
    assert connection.scalar(text("SELECT count(*) FROM action_event")) == 0

    # Check the changed tables, not unrelated migration-only request/execution indexes.
    context = MigrationContext.configure(
        connection,
        opts={
            "include_object": lambda obj, name, type_, reflected, compare_to: (
                type_ != "index" and (type_ != "table" or name in {"action_decision", "action_event", "action_request"})
            )
        },
    )
    assert compare_metadata(context, Base.metadata) == []


async def test_migration_round_trip_preserves_carried_data_and_matches_metadata(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_round_trip)


if __name__ == "__main__":
    pytest_bazel.main()
