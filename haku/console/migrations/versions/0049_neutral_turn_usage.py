"""A turn's token accounting stops being one CLI's payload.

`session_turns.usage` held Claude's own `usage` sub-object verbatim, so "this exchange used X
tokens" meant "whatever that one CLI called it". These three columns are the neutral shape the
backend adapter produced, beside `cost_usd` and `duration_ms`, which the store used to mine out of
the same payload by key name.

**They are counters, and counters sum**, which is what a turn spanning several invocations will
need: a session's token total is a `SUM` over rows rather than a fold over JSON. `cost_usd` sums
too. `duration_ms` deliberately does not — wall clock of invocations that may overlap is not their
sum — so an exchange's elapsed time stays `ended_at - started_at`.

The backfill reads the JSONB the columns replace, which is why it can be exact rather than
archaeological: `cache_read_input_tokens` is the key Claude spells the cached counter with, and a
key the payload never carried is 0, which is what an unreported counter meant. Rows carrying
a cost or a duration but no usage object get zeros rather than NULLs, so no historical exchange
loses its cost to the reader's "usage present" test.

**Additive on purpose.** A replica on the previous image (README § Perimeter / deploy) neither
selects nor writes these columns and keeps writing `usage`, which this release leaves in place and
stops reading; the check constraint it cannot violate, since it never names a counter. Dropping
`usage` is the contract half, tombstoned on the column in `database_schema.py`.

Revision ID: 0049
Revises: 0048
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_turns"
_COUNTERS = ("input_tokens", "output_tokens", "cached_input_tokens")


def upgrade() -> None:
    for column in _COUNTERS:
        op.add_column(_TABLE, sa.Column(column, sa.BigInteger(), nullable=True))
    op.execute(
        sa.text("""
        UPDATE session_turns
           SET input_tokens = COALESCE((usage ->> 'input_tokens')::bigint, 0),
               output_tokens = COALESCE((usage ->> 'output_tokens')::bigint, 0),
               cached_input_tokens = COALESCE((usage ->> 'cache_read_input_tokens')::bigint, 0)
         WHERE usage IS NOT NULL OR cost_usd IS NOT NULL OR duration_ms IS NOT NULL
        """)
    )
    op.create_check_constraint(
        "ck_session_turns_usage_counters",
        _TABLE,
        "(input_tokens IS NULL) = (output_tokens IS NULL) AND (input_tokens IS NULL) = (cached_input_tokens IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_session_turns_usage_counters", _TABLE, type_="check")
    for column in _COUNTERS:
        op.drop_column(_TABLE, column)
