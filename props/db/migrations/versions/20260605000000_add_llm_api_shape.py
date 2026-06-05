"""Add LLM API shape columns.

Revision ID: 20260605000000
Revises: 20260604000001
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605000000"
down_revision: str | None = "20260604000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_metadata", sa.Column("api_shape", sa.String(), nullable=False, server_default="responses"))
    op.create_check_constraint(
        "model_metadata_api_shape_check", "model_metadata", "api_shape IN ('responses', 'chat_completions')"
    )

    op.add_column("llm_requests", sa.Column("api_shape", sa.String(), nullable=False, server_default="responses"))
    op.create_check_constraint(
        "llm_requests_api_shape_check", "llm_requests", "api_shape IN ('responses', 'chat_completions')"
    )

    op.execute("""
        CREATE OR REPLACE VIEW llm_request_costs AS
        SELECT
            r.id,
            r.agent_run_id,
            r.model,
            r.input_tokens,
            r.cached_input_tokens,
            r.output_tokens,
            r.latency_ms,
            r.created_at,
            (
                (COALESCE(r.input_tokens, 0) - COALESCE(r.cached_input_tokens, 0))
                    * COALESCE(m.input_usd_per_1m_tokens, 0) / 1000000.0
                + COALESCE(r.cached_input_tokens, 0)
                    * COALESCE(m.cached_input_usd_per_1m_tokens, 0) / 1000000.0
                + COALESCE(r.output_tokens, 0)
                    * COALESCE(m.output_usd_per_1m_tokens, 0) / 1000000.0
            ) AS cost_usd
        FROM llm_requests r
        LEFT JOIN model_metadata m ON r.model = m.model_id
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW llm_request_costs AS
        SELECT
            r.id,
            r.agent_run_id,
            r.model,
            r.input_tokens,
            r.cached_input_tokens,
            r.output_tokens,
            r.latency_ms,
            r.created_at,
            COALESCE(
                (r.input_tokens - COALESCE(r.cached_input_tokens, 0))
                    * m.input_usd_per_1m_tokens / 1000000.0
                + COALESCE(r.cached_input_tokens, 0)
                    * m.cached_input_usd_per_1m_tokens / 1000000.0
                + r.output_tokens
                    * m.output_usd_per_1m_tokens / 1000000.0,
                0
            ) AS cost_usd
        FROM llm_requests r
        LEFT JOIN model_metadata m ON r.model = m.model_id
    """)
    op.drop_constraint("llm_requests_api_shape_check", "llm_requests", type_="check")
    op.drop_column("llm_requests", "api_shape")
    op.drop_constraint("model_metadata_api_shape_check", "model_metadata", type_="check")
    op.drop_column("model_metadata", "api_shape")
