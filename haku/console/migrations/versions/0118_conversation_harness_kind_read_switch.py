"""Switch conversation reads from ``runtime_kind`` to ``harness_kind``.

C4d contract release 1 of the #4772 vocabulary collapse. The expand release (0114) added and
backfilled ``harness_kind`` while keeping it nullable, because pre-expand replicas could still
insert rows without that column. Once every replica has the expand image, backfill any remaining
roll-window rows, make the new column required, and switch the application reads to it.

Both columns remain mapped and dual-written for the next compatibility release. The conversation
identity trigger is rebuilt to protect both names while they coexist; the backfill happens first so
the new trigger does not reject the NULL-to-value migration itself.

Revision ID: 0118
Revises: 0113
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0118"
down_revision: str | None = "0113"
branch_labels: str | None = None
depends_on: str | None = None

_TRIGGER = "conversation_identity_immutable"
_FUNCTION = "prevent_conversation_identity_update"


def _drop_identity_guard() -> None:
    op.execute(f"DROP TRIGGER {_TRIGGER} ON conversation")
    op.execute(f"DROP FUNCTION {_FUNCTION}()")


def _create_identity_guard() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {_FUNCTION}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.agent_id IS DISTINCT FROM OLD.agent_id
                   OR NEW.access_profile_id IS DISTINCT FROM OLD.access_profile_id
                   OR NEW.runtime_kind IS DISTINCT FROM OLD.runtime_kind
                   OR NEW.harness_kind IS DISTINCT FROM OLD.harness_kind THEN
                    RAISE EXCEPTION 'conversation identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_TRIGGER}
            BEFORE UPDATE OF agent_id, access_profile_id, runtime_kind, harness_kind
            ON conversation
            FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()
            """
        )
    )


def upgrade() -> None:
    # The old 0093 trigger does not watch harness_kind, so let it permit this data-only backfill.
    op.execute("UPDATE conversation SET harness_kind = runtime_kind WHERE harness_kind IS NULL")
    op.alter_column("conversation", "harness_kind", existing_type=sa.Text(), nullable=False)

    _drop_identity_guard()
    _create_identity_guard()


def downgrade() -> None:
    _drop_identity_guard()
    op.alter_column("conversation", "harness_kind", existing_type=sa.Text(), nullable=True)

    # Restore the pre-read-switch guard, which is the contract of the schema at 0117.
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {_FUNCTION}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.agent_id IS DISTINCT FROM OLD.agent_id
                   OR NEW.access_profile_id IS DISTINCT FROM OLD.access_profile_id
                   OR NEW.runtime_kind IS DISTINCT FROM OLD.runtime_kind THEN
                    RAISE EXCEPTION 'conversation identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_TRIGGER}
            BEFORE UPDATE OF agent_id, access_profile_id, runtime_kind
            ON conversation
            FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()
            """
        )
    )
