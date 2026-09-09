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
from x.agentplane.action_service.models import ActionRequestInput, DecisionInput, Principal, PrincipalRole, Verdict


def _round_trip(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["connection"] = connection
    config.attributes["target_metadata"] = Base.metadata
    command.downgrade(config, "0005_structured_action")
    # Historical column spelling exists only to verify upgrade/downgrade compatibility.
    assert connection.scalar(text("SELECT private_reason FROM action_decision")) == "Original note — preserved."
    connection.execute(text("UPDATE action_decision SET private_reason = 'Existing deployed note — preserved.'"))
    command.upgrade(config, "head")
    assert connection.scalar(text("SELECT decision_note FROM action_decision")) == "Existing deployed note — preserved."
    assert connection.scalar(text("SELECT count(*) FROM action_event WHERE actor_principal IS NOT NULL")) == 0
    # Check the changed tables, not unrelated migration-only request/execution indexes.
    context = MigrationContext.configure(
        connection,
        opts={
            "include_object": lambda obj, name, type_, reflected, compare_to: (
                type_ != "table" or name in {"action_decision", "action_event"}
            )
        },
    )
    assert compare_metadata(context, Base.metadata) == []


async def test_decision_note_migration_preserves_data_and_matches_metadata(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    caller = Principal(issuer="test", subject="caller", role=PrincipalRole.CALLER)
    operator = Principal(issuer="test", subject="operator", role=PrincipalRole.OPERATOR)
    pending, _ = await store.submit(
        ActionRequestInput(
            idempotency_key="migration-request", action=ActionIdentity(group="test", name="echo"), arguments={}
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
    caller_view = await store.get(pending.id, caller)
    operator_view = await store.get(pending.id, operator)
    assert caller_view.decision == operator_view.decision
    assert caller_view.decision is not None
    assert caller_view.decision.decision_note == "Existing deployed note — preserved."
    assert caller_view.execution is None


if __name__ == "__main__":
    pytest_bazel.main()
