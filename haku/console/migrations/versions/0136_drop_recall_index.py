"""Drop the retired Console Recall schema and its indexed data.

No Console image since the Recall integration's removal maps or validates `recall_index`.
The `vector` extension stays: it is CNPG-provisioned, and 0135 still needs `halfvec` on a fresh
database.

Revision ID: 0136
Revises: 0135
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0136"
down_revision: str | None = "0135"
branch_labels: str | None = None
depends_on: str | None = None

_SCHEMA = "recall_index"


def upgrade() -> None:
    # Referencing tables first; the schema drop is RESTRICT so any unexpected object fails loudly.
    for table in ("content_embeddings", "git_chunks", "git_sync_state", "git_tip", "contents", "indexes"):
        op.drop_table(table, schema=_SCHEMA)
    op.execute(sa.schema.DropSchema(_SCHEMA))


def downgrade() -> None:
    raise RuntimeError("0136 deleted the Console Recall index data; it cannot be restored")
