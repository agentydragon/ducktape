"""Allow 'anthropic' in the api_shape check constraints.

The api_shape columns (added in 20260605000000) constrained values to
('responses', 'chat_completions'). The LLMApiShape enum gained 'anthropic' (z.ai's
GLM served through the Anthropic Messages shape via props-llm-proxy /v1/messages),
so the DB constraints must accept it or model_metadata sync of anthropic models fails.

Revision ID: 20260608000000
Revises: 20260605000000
Create Date: 2026-06-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260608000000"
down_revision: str | None = "20260605000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHAPES_WITH_ANTHROPIC = "api_shape IN ('responses', 'chat_completions', 'anthropic')"
_SHAPES_WITHOUT_ANTHROPIC = "api_shape IN ('responses', 'chat_completions')"


def upgrade() -> None:
    op.drop_constraint("model_metadata_api_shape_check", "model_metadata", type_="check")
    op.create_check_constraint("model_metadata_api_shape_check", "model_metadata", _SHAPES_WITH_ANTHROPIC)
    op.drop_constraint("llm_requests_api_shape_check", "llm_requests", type_="check")
    op.create_check_constraint("llm_requests_api_shape_check", "llm_requests", _SHAPES_WITH_ANTHROPIC)


def downgrade() -> None:
    op.drop_constraint("llm_requests_api_shape_check", "llm_requests", type_="check")
    op.create_check_constraint("llm_requests_api_shape_check", "llm_requests", _SHAPES_WITHOUT_ANTHROPIC)
    op.drop_constraint("model_metadata_api_shape_check", "model_metadata", type_="check")
    op.create_check_constraint("model_metadata_api_shape_check", "model_metadata", _SHAPES_WITHOUT_ANTHROPIC)
