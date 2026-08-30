"""Finish the tool-call decision contract after old replicas have drained.

The previous release left ``denial_reason`` physically present but stopped mapping or writing it.
This migration reconciles rows an old API replica may have written during the rolling overlap, then
removes the column. It must not be deployed until the expand/frontend soak gates have passed.

Revision ID: 0126
Revises: 0125
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0126"
down_revision: str | None = "0125"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Reconcile decisions made by an old API replica after 0124 ran. Old denials only populated
    # denial_reason; old approvals populated neither new field, so recover their Operator source
    # using the same principal/Agent-owner relation as the expand migration.
    op.execute(
        sa.text(
            """
            UPDATE mcp_tool_calls AS call
            SET decision_note = COALESCE(call.decision_note, NULLIF(btrim(call.denial_reason), '')),
                decision_operator_id = CASE
                    WHEN call.status = 'denied'
                         AND COALESCE(call.auto_approval_evaluation, '') NOT LIKE 'denied:%'
                    THEN COALESCE(call.decision_operator_id, principal.operator_id, agent.owner_operator_id)
                    ELSE call.decision_operator_id
                END
            FROM mcp_tool_call_principals AS principal
            LEFT JOIN credential_bindings AS binding
              ON binding.binding_id = principal.binding_id
            LEFT JOIN agents AS agent
              ON agent.agent_id = binding.agent_id
            WHERE principal.tool_call_id = call.tool_call_id
              AND (
                  (call.status = 'denied' AND call.denial_reason IS NOT NULL)
                  OR (call.status IN ('running', 'ok', 'error')
                      AND call.approved_at IS NOT NULL
                      AND call.approval_policy_id IS NULL
                      AND call.decision_operator_id IS NULL)
              )
            """
        )
    )
    # Some historical manual decisions have incomplete provenance even though this database has
    # exactly one Operator. That sole row is an unambiguous attribution; do not apply this repair
    # when there are zero or multiple Operators, so the contract remains fail-closed there.
    op.execute(
        sa.text(
            """
            UPDATE mcp_tool_calls AS call
            SET decision_operator_id = sole.operator_id
            FROM operators AS sole
            WHERE call.decision_operator_id IS NULL
              AND (
                  (call.status = 'denied'
                   AND COALESCE(call.auto_approval_evaluation, '') NOT LIKE 'denied:%')
                  OR (call.status IN ('running', 'ok', 'error')
                      AND call.approved_at IS NOT NULL
                      AND call.approval_policy_id IS NULL)
              )
              AND (SELECT count(*) FROM operators) = 1
            """
        )
    )
    # The replacement attribution FK and the legacy principal validation trigger are deferred, and
    # PostgreSQL rejects DDL on a table with pending trigger events. Validate all deferred
    # constraints now so the column drop below can proceed in this transaction.
    op.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
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
                    AND call.decision_operator_id IS NULL
                ) THEN
                    RAISE EXCEPTION
                        'cannot remove legacy decision field: a manual tool-call decision has no operator';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM mcp_tool_calls
                    WHERE status = 'denied'
                      AND COALESCE(auto_approval_evaluation, '') LIKE 'denied:%'
                      AND decision_operator_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION
                        'cannot remove legacy decision field: an automatic denial has an operator';
                END IF;
            END;
            $$
            """
        )
    )
    op.drop_column("mcp_tool_calls", "denial_reason")


def downgrade() -> None:
    op.add_column("mcp_tool_calls", sa.Column("denial_reason", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE mcp_tool_calls
            SET denial_reason = decision_note
            WHERE status = 'denied'
            """
        )
    )
