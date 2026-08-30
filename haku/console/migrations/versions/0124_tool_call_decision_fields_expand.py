"""Expand tool-call decisions with a neutral note and operator attribution.

The old ``denial_reason`` column conflates an automatic denial explanation with an Operator's
optional note.  Add the replacement fields while old API replicas and static bundles may still
serve.  The old column remains populated as a compatibility projection until the contract release
removes it.

For a decided call, ``decision_operator_id`` is the source discriminator: an Operator decision has
the authenticated Operator's id, while an automatic decision leaves it NULL.  Historical manual
decisions can recover that id from the existing actor-scope ownership relation because only the
principal Operator or Agent owner could make the decision.

Revision ID: 0124
Revises: 0123
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0124"
down_revision: str | None = "0123"
branch_labels: str | None = None
depends_on: str | None = None

_DECISION_NOTE_LENGTH = 4096
_DECISION_NOTE_CHECK = "ck_mcp_tool_calls_decision_note_length"
_DECISION_OPERATOR_FK = "fk_mcp_tool_calls_decision_operator"


def upgrade() -> None:
    # Fail before changing the schema if existing denial text cannot fit the bounded replacement
    # field. This makes the migration failure actionable and leaves the old release deployable.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM mcp_tool_calls
                    WHERE denial_reason IS NOT NULL
                      AND char_length(btrim(denial_reason)) > 4096
                ) THEN
                    RAISE EXCEPTION 'historical tool-call denial reason exceeds 4096 characters';
                END IF;
            END;
            $$
            """
        )
    )
    op.add_column("mcp_tool_calls", sa.Column("decision_note", sa.Text(), nullable=True))
    op.add_column("mcp_tool_calls", sa.Column("decision_operator_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        _DECISION_OPERATOR_FK,
        "mcp_tool_calls",
        "operators",
        ["decision_operator_id"],
        ["operator_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        _DECISION_NOTE_CHECK,
        "mcp_tool_calls",
        f"decision_note IS NULL OR char_length(decision_note) <= {_DECISION_NOTE_LENGTH}",
    )

    # Automatic denials are born denied by the submit path and carry an evaluation beginning with
    # `denied:`. Keep the existing text as the new neutral decision annotation.
    op.execute(
        sa.text(
            """
            UPDATE mcp_tool_calls
            SET decision_note = NULLIF(btrim(denial_reason), '')
            WHERE status = 'denied'
              AND auto_approval_evaluation LIKE 'denied:%'
            """
        )
    )

    # A call's current operator scope has exactly one eligible Operator: the principal's explicit
    # operator, or the owner of the Agent behind its credential binding. Validate that assumption
    # before using the relation to recover historical manual decision attribution.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM mcp_tool_calls AS call
                    JOIN mcp_tool_call_principals AS principal
                      ON principal.tool_call_id = call.tool_call_id
                    LEFT JOIN credential_bindings AS binding
                      ON binding.binding_id = principal.binding_id
                    LEFT JOIN agents AS agent
                      ON agent.agent_id = binding.agent_id
                    WHERE (
                        (call.status = 'denied'
                         AND COALESCE(call.auto_approval_evaluation, '') NOT LIKE 'denied:%')
                        OR (call.status IN ('running', 'ok', 'error')
                            AND call.approved_at IS NOT NULL
                            AND call.approval_policy_id IS NULL)
                    )
                    AND COALESCE(principal.operator_id, agent.owner_operator_id) IS NULL
                ) THEN
                    RAISE EXCEPTION
                        'cannot recover a unique operator for a historical manual tool-call decision';
                END IF;
            END;
            $$
            """
        )
    )

    # Manual denials and approvals are attributed to the same Operator that the current ledger
    # scope permits to make the transition. Manual denial text is copied from the old field; the old
    # value remains in place for old frontend bundles during this expand release.
    op.execute(
        sa.text(
            """
            UPDATE mcp_tool_calls AS call
            SET decision_note = NULLIF(btrim(call.denial_reason), ''),
                decision_operator_id = COALESCE(principal.operator_id, agent.owner_operator_id)
            FROM mcp_tool_call_principals AS principal
            LEFT JOIN credential_bindings AS binding
              ON binding.binding_id = principal.binding_id
            LEFT JOIN agents AS agent
              ON agent.agent_id = binding.agent_id
            WHERE principal.tool_call_id = call.tool_call_id
              AND call.status = 'denied'
              AND COALESCE(call.auto_approval_evaluation, '') NOT LIKE 'denied:%'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE mcp_tool_calls AS call
            SET decision_operator_id = COALESCE(principal.operator_id, agent.owner_operator_id)
            FROM mcp_tool_call_principals AS principal
            LEFT JOIN credential_bindings AS binding
              ON binding.binding_id = principal.binding_id
            LEFT JOIN agents AS agent
              ON agent.agent_id = binding.agent_id
            WHERE principal.tool_call_id = call.tool_call_id
              AND call.status IN ('running', 'ok', 'error')
              AND call.approved_at IS NOT NULL
              AND call.approval_policy_id IS NULL
            """
        )
    )

    # Do not let historical data silently violate the application-level source invariant.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM mcp_tool_calls
                    WHERE decision_note IS NOT NULL
                      AND char_length(decision_note) > 4096
                ) THEN
                    RAISE EXCEPTION 'historical tool-call decision note exceeds 4096 characters';
                END IF;
            END;
            $$
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint(_DECISION_NOTE_CHECK, "mcp_tool_calls", type_="check")
    op.drop_constraint(_DECISION_OPERATOR_FK, "mcp_tool_calls", type_="foreignkey")
    op.drop_column("mcp_tool_calls", "decision_operator_id")
    op.drop_column("mcp_tool_calls", "decision_note")
