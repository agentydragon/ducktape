"""Drop the turn usage columns the ORM stopped mapping. **Destructive.**

The contract half of #4306, and the third step of the three the README's Perimeter / deploy section
requires: #4278 stopped writing `session_turns.{input_tokens,output_tokens,cached_input_tokens,
cost_usd,duration_ms}`, #4306 stopped mapping them, and this drops them. An ORM-mapped column is
named in every `SELECT` SQLAlchemy emits for it whether or not any code reads the attribute, so the
middle step is what makes this safe — and it is gated on the unmapping having converged, not on a
release having elapsed, because `maxUnavailable: 0` lets a stalled roll keep an old replica serving.
Checked before this landed: both `haku-console` pods on `devel-20260817104540-3d909fb`, one tag, and
#4306 (`dcd847ae48`) an ancestor of `3d909fb`.

`ck_session_turns_usage_counters` goes with them. It tied the three counters together so that a row
counting input tokens and not output ones was unrepresentable; with no counters there is nothing
left for it to tie. Postgres would drop it along with the first column anyway — naming it is so the
migration says what it does.

The numbers are not lost with the columns: they were read off the `result` frame's payload, which
stays whole in `session_frames`, and the turn's frame range points at it (console `x/README.md`
§ The payload is evidence).

The column types are spelled out here rather than imported from `0032`/`0049`, for the reason `0041`
gives: a migration is a point-in-time statement about the database, so it must not change meaning
when another file is edited.

Revision ID: 0069
Revises: 0068
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_turns"
_CONSTRAINT = "ck_session_turns_usage_counters"
_COUNTERS = ("input_tokens", "output_tokens", "cached_input_tokens")


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    for column in (*_COUNTERS, "cost_usd", "duration_ms"):
        op.drop_column(_TABLE, column)


def downgrade() -> None:
    """Puts the columns back empty, which is what an earlier image expects to find.

    Nothing reconstructs the values: the release that wrote them went three releases ago, so every
    row this could restore has been NULL in them since long before the drop.
    """
    for column in _COUNTERS:
        op.add_column(_TABLE, sa.Column(column, sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True))
    op.add_column(_TABLE, sa.Column("duration_ms", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "(input_tokens IS NULL) = (output_tokens IS NULL) AND (input_tokens IS NULL) = (cached_input_tokens IS NULL)",
    )
