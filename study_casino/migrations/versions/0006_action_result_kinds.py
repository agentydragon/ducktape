"""Tag historical server-action results before requiring discrimination.

Deploy phase-2 writers before this revision: they always write a result kind.
Phase-1 writers could otherwise insert an untagged row after the backfill.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Historical mapping, intentionally independent of application model imports.
_RESULT_KINDS = {
    "session.complete": "session.complete",
    "session.add_past": "session.add_past",
    "session.edit": "session.credits_delta",
    "session.delete": "session.credits_delta",
    "convert": "convert",
    "prize.create": "prize.create",
    "prize.delete": "prize.delete",
    "prize.update": "prize.update",
    "prize.redeem": "prize.redeem",
    "changelog.ack": "changelog.ack",
    "data.import": "data.import",
    "data.reset": "data.reset",
    "casino.slots.spin": "casino.slots.spin",
    "casino.roulette.spin": "casino.roulette.spin",
    "blackjack.deal": "blackjack.hand_state",
    "blackjack.hit": "blackjack.hand_state",
    "blackjack.stand": "blackjack.hand_state",
    "blackjack.double": "blackjack.hand_state",
}


def upgrade() -> None:
    connection = op.get_bind()
    unknown = (
        connection.execute(
            sa.text(
                """
            SELECT DISTINCT action_type FROM ledger_events
            WHERE source = 'server_action'
              AND result_json::jsonb ->> 'kind' IS NULL
              AND action_type NOT IN :known_types
            """
            ).bindparams(sa.bindparam("known_types", expanding=True)),
            {"known_types": list(_RESULT_KINDS)},
        )
        .scalars()
        .all()
    )
    if unknown:
        raise RuntimeError(f"Cannot infer result kinds for server-action types: {unknown}")

    connection.execute(
        sa.text(
            """
            UPDATE ledger_events
            SET result_json = (
                result_json::jsonb || jsonb_build_object('kind', CAST(:kind AS text))
            )::text
            WHERE source = 'server_action' AND action_type = :action_type
              AND result_json::jsonb ->> 'kind' IS NULL
            """
        ),
        [{"action_type": action_type, "kind": kind} for action_type, kind in _RESULT_KINDS.items()],
    )


def downgrade() -> None:
    # Tags are additive and phase-1/2 readers accept them. Retain them on rollback
    # so downgrade cannot invalidate results written while phase 3 was active.
    pass
