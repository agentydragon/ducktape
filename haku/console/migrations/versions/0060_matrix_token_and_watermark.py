"""One row per owner: the cached Matrix token and the sync watermark stop sharing a row.

`matrix_sync_state` held a credential cache and a durable watermark in one row, both nullable, so
"the row exists" said nothing and each of its two writers — `save_token` from inside the pacer's
queued send, `_advance` from the sync pass — had to be able to create it. `matrix_access_token` and
`matrix_sync_watermark` give each writer its own row with a `NOT NULL` value, so an absent row means
one definite thing: no token cached, nothing finished with yet.

Both existing values are carried across; a NULL column becomes no row, which is the same state
said in the new vocabulary.

**Additive, and safe for the length of a roll** (console README § Perimeter / deploy). A replica on
the previous image keeps reading and writing `matrix_sync_state`, which is untouched here. The two
images do diverge while both are up, in the direction that costs rather than loses:

- **The watermark.** Only the `MXSY` lock holder advances one, so during the roll that is still the
  old replica, writing the old table; the new leader resumes from the copy taken here. That copy is
  behind, never ahead, so the batches acknowledged in between are delivered a second time.
  Re-delivery is the failure R2.5 requires the system to survive; a skip is the one it forbids.
- **The cached token.** The pacer runs on every replica, so the two images cache to different
  tables. Each validates its own copy with `/whoami` and logs in only if that fails, which costs at
  most an extra login or two against Synapse's `/login` limit for the length of the roll.

`matrix_sync_state` is dropped a release later, gated on this one having converged —
`database_schema.UNMAPPED_TABLES_PENDING_DROP` carries the tombstone.

Revision ID: 0060
Revises: 0059
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_access_token",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("access_token", sa.Text(), nullable=False),
    )
    op.create_table(
        "matrix_sync_watermark",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("next_batch", sa.Text(), nullable=False),
    )
    op.execute(
        sa.text(
            "INSERT INTO matrix_access_token (user_id, access_token) "
            "SELECT user_id, access_token FROM matrix_sync_state WHERE access_token IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO matrix_sync_watermark (user_id, next_batch) "
            "SELECT user_id, next_batch FROM matrix_sync_state WHERE next_batch IS NOT NULL"
        )
    )


def downgrade() -> None:
    # `matrix_sync_state` still holds what was copied out of it, so what is lost is whatever moved
    # after this ran: the console falls back to the older watermark and re-delivers, and to the
    # older token, which it will replace by logging in once if the homeserver has stopped taking it.
    op.drop_table("matrix_sync_watermark")
    op.drop_table("matrix_access_token")
