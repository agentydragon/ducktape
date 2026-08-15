"""Record the branch's own tip beside what is indexed from it.

`git_sync_state` only had a row once a sync had completed, so before the first one `index_status`
could say nothing about the haku-state corpus — "never configured", "cannot reach the remote",
"indexing right now" and "behind by a commit" were one absent object. A sweep now writes what it
saw on every tick, into the same row, and the indexed columns become nullable because they only
become true later.

The check keeps that half all-or-nothing, so the relaxation cannot turn into a commit recorded
without the regime it was indexed under.

Additive for the length of a roll: the previous release only ever writes all four indexed columns
together, and only reads a row it wrote.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | None = None
depends_on: str | None = None

SCHEMA = "state_index"
_TABLE = "git_sync_state"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("remote_commit", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column(_TABLE, sa.Column("remote_seen_at", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    for column in ("commit_sha", "chunker_key", "model_key"):
        op.alter_column(_TABLE, column, existing_type=sa.Text(), nullable=True, schema=SCHEMA)
    op.alter_column(_TABLE, "synced_at", existing_type=sa.DateTime(timezone=True), nullable=True, schema=SCHEMA)
    op.create_check_constraint(
        "ck_git_sync_state_indexed_half",
        _TABLE,
        "(commit_sha IS NULL) = (chunker_key IS NULL)"
        " AND (commit_sha IS NULL) = (model_key IS NULL)"
        " AND (commit_sha IS NULL) = (synced_at IS NULL)",
        schema=SCHEMA,
    )
