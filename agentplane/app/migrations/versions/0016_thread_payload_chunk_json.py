"""Store thread payload chunks as JSON strings so text can contain escaped U+0000.

Databases that applied `0005` while it created this column as `text` hold bare chunk text, plus the
JSON string scalars the app has written since it started writing them. Databases created while `0005`
created it as `json` already hold only the latter, and the same conversion leaves them unchanged.

A bare chunk is told from a written one by being a JSON string scalar, so a bare chunk that is
itself exactly a quoted JSON string keeps its quotes out of the decoded text.
"""

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_thread_payload_chunk_json"
down_revision = "0015_event_payload_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "thread_payload_chunk",
        "text",
        type_=postgresql.JSON(),
        # `::text` makes one expression serve both column types; `IS JSON SCALAR` is also true of 123.
        postgresql_using="""
            CASE WHEN "text"::text IS JSON SCALAR AND left("text"::text, 1) = '"'
                 THEN "text"::json
                 ELSE to_json("text"::text)
            END
        """,
    )


def downgrade() -> None:
    # This cast fails if any chunk contains U+0000, which `text` cannot represent.
    op.alter_column(
        "thread_payload_chunk",
        "text",
        existing_type=postgresql.JSON(),
        type_=postgresql.TEXT(),
        postgresql_using="""("text"::jsonb) #>> '{}'""",
    )
