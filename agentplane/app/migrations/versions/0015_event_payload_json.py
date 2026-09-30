"""Store event payloads as JSON so strings can contain escaped U+0000."""

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_event_payload_json"
down_revision = "0014_operator_session_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "event",
        "payload",
        existing_type=postgresql.JSONB(),
        type_=postgresql.JSON(),
        postgresql_using="payload::text::json",
    )


def downgrade() -> None:
    # This cast fails if any event contains U+0000, which JSONB cannot represent.
    op.alter_column(
        "event",
        "payload",
        existing_type=postgresql.JSON(),
        type_=postgresql.JSONB(),
        postgresql_using="payload::text::jsonb",
    )
