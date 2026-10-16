"""Migration metadata for the retired Runner Gateway Session registry table.

Serving code no longer reads or writes this table. It remains in migration metadata until the
registry-free image has rolled out and a separate approved database-retirement migration lands.
"""

import sqlalchemy as sa
from sqlalchemy import MetaData, Table

metadata = MetaData()
Table(
    "session",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
    sa.Column("sandbox_namespace", sa.String(), nullable=False),
    sa.Column("sandbox_name", sa.String(), nullable=False),
    sa.Column("sandbox_uid", sa.Uuid(), nullable=True),
)
