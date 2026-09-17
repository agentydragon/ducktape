"""Real-Postgres compatibility for the data-preserving human-note column rename."""

from pathlib import Path

import pytest_bazel
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.db import ActionStore, Base, make_sessionmaker
from x.agentplane.action_service.models import (
    ActionRequestInput,
    CallerPrincipal,
    DecisionInput,
    OperatorPrincipal,
    Verdict,
)
from x.agentplane.subjects import ServiceAccountRef


def _round_trip(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["connection"] = connection
    config.attributes["target_metadata"] = Base.metadata
    command.downgrade(config, "0005_structured_action")
    # Historical column spelling exists only to verify upgrade/downgrade compatibility.
    assert connection.scalar(text("SELECT private_reason FROM action_decision")) == "Original note — preserved."
    connection.execute(text("UPDATE action_decision SET private_reason = 'Existing deployed note — preserved.'"))
    # Up to the last migration that carries data forward; `0016` deliberately does not.
    command.upgrade(config, "0015_action_request_context")
    assert connection.scalar(text("SELECT decision_note FROM action_decision")) == "Existing deployed note — preserved."
    assert connection.scalar(text("SELECT count(*) FROM action_event WHERE actor_principal IS NOT NULL")) == 0
    assert connection.scalar(text("SELECT count(*) FROM action_request WHERE external_grant IS NOT NULL")) == 0
    # A request submitted before callers could supply context must come back with a renderable
    # title, not an empty one, or the approval surfaces show a blank line for it forever.
    assert connection.scalar(text("SELECT title FROM action_request"))
    assert connection.scalar(text("SELECT count(*) FROM action_request WHERE description IS NOT NULL")) == 0
    # `0016` stores a principal as its own fields and drops what the old encoding held rather than
    # parsing it apart, so the rows this seeded do not survive it. That is the documented behaviour.
    command.upgrade(config, "head")
    assert connection.scalar(text("SELECT count(*) FROM action_request")) == 0
    assert connection.scalar(text("SELECT count(*) FROM action_decision")) == 0

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


async def test_decision_note_migration_preserves_data_and_matches_metadata(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    caller = CallerPrincipal(account=ServiceAccountRef(namespace="agentplane-test", name="test-caller"))
    operator = OperatorPrincipal(issuer="test", subject="operator")
    pending = await store.submit(
        ActionRequestInput(
            idempotency_key="migration-request",
            title="test title for migration-request",
            action=ActionIdentity(group="test", name="echo"),
            arguments={},
        ),
        caller,
    )
    await store.decide(
        pending.id,
        DecisionInput(
            verdict=Verdict.DENY,
            expected_version=pending.version,
            idempotency_key="migration-decision",
            decision_note="Original note — preserved.",
        ),
        operator,
        provider="human_operator",
    )
    async with engine.begin() as connection:
        await connection.run_sync(_round_trip)
    # Nothing is read back through the ORM: `0016` emptied the request on the way back up, which the
    # round trip asserted where it happens.


if __name__ == "__main__":
    pytest_bazel.main()
