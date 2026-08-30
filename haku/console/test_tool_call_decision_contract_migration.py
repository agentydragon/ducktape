"""0126 drops `denial_reason` after backfilling `decision_note`/`decision_operator_id`.

Regression cover for a production-only failure: the migration's own UPDATEs against
`mcp_tool_calls` queue that table's DEFERRABLE INITIALLY DEFERRED constraint triggers
(`fk_mcp_tool_calls_decision_operator` from 0124, and 0081's `ctrg_haku_0009_call_has_principal`),
and PostgreSQL rejects DDL on a table with pending trigger events. A fresh, row-less test database
never queues any such event, so this only reproduces with a pre-existing row — exactly the shape
prod hit and CI's empty-DB migration coverage cannot.

Temporary per `AGENTS.md` § "Do not keep tests for old migrations": delete once the chain is
roughly five revisions past 0126.
"""

from __future__ import annotations

import datetime
from uuid import uuid4

import pytest_bazel
from sqlalchemy import create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 30, tzinfo=datetime.UTC)


def test_0126_backfills_and_drops_denial_reason_with_an_existing_denied_row(db_url: str) -> None:
    apply_migrations(db_url, "0125")
    engine = create_engine(sync_database_url(db_url))
    try:
        operator_id = uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"
                ),
                {"id": operator_id, "n": _NOW},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_calls (
                        tool_call_id, server_id, tool_name, status,
                        created_at, updated_at, arguments_json, rationale, denial_reason
                    ) VALUES (
                        'legacy-denial', 'server', 'tool', 'denied',
                        :n, :n, '{}'::jsonb, 'manual denial test', 'not today'
                    )
                    """
                ),
                {"n": _NOW},
            )
            conn.execute(
                text(
                    "INSERT INTO mcp_tool_call_principals (tool_call_id, operator_id)"
                    " VALUES ('legacy-denial', :operator_id)"
                ),
                {"operator_id": operator_id},
            )

        apply_migrations(db_url)  # must not raise "pending trigger events" on the column drop

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'mcp_tool_calls'"
                        " AND column_name = 'denial_reason'"
                    )
                ).scalar_one()
                == 0
            )
            decision_note, decision_operator_id = conn.execute(
                text(
                    "SELECT decision_note, decision_operator_id FROM mcp_tool_calls"
                    " WHERE tool_call_id = 'legacy-denial'"
                )
            ).one()
            assert decision_note == "not today"
            assert decision_operator_id == operator_id
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
