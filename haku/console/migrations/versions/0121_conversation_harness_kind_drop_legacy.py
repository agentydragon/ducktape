"""Drop the legacy conversation harness discriminator.

C4d contract release 3 of #4772. Every current writer has stopped mentioning ``runtime_kind`` and
the compatibility window has elapsed, so remove the old column and CHECK. Rebuild the conversation
identity guard to protect only the canonical ``harness_kind`` column before the first post-drop
update can occur.

Revision ID: 0121
Revises: 0120
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0121"
down_revision: str | None = "0120"
branch_labels: str | None = None
depends_on: str | None = None

_TRIGGER = "conversation_identity_immutable"
_FUNCTION = "prevent_conversation_identity_update"
_RUNTIME_CONSTRAINT = "ck_conversation_runtime_kind"


def _drop_identity_guard() -> None:
    op.execute(f"DROP TRIGGER {_TRIGGER} ON conversation")
    op.execute(f"DROP FUNCTION {_FUNCTION}()")


def _create_harness_identity_guard() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {_FUNCTION}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.agent_id IS DISTINCT FROM OLD.agent_id
                   OR NEW.access_profile_id IS DISTINCT FROM OLD.access_profile_id
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
            BEFORE UPDATE OF agent_id, access_profile_id, harness_kind
            ON conversation
            FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()
            """
        )
    )


def upgrade() -> None:
    _drop_identity_guard()
    op.drop_constraint(_RUNTIME_CONSTRAINT, "conversation", type_="check")
    op.drop_column("conversation", "runtime_kind")
    _create_harness_identity_guard()


def downgrade() -> None:
    _drop_identity_guard()
    op.add_column("conversation", sa.Column("runtime_kind", sa.Text(), nullable=True))
    op.execute("UPDATE conversation SET runtime_kind = harness_kind")
    op.alter_column("conversation", "runtime_kind", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        _RUNTIME_CONSTRAINT, "conversation", "runtime_kind IN ('claude_code', 'codex_app_server')"
    )
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
