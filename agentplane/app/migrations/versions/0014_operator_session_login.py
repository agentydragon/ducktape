"""An operator session's login moves out of its JSON payload into typed columns, which check
constraints keep from being half-written.

Existing sessions are dropped rather than converted, in both directions: their operators log in again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_operator_session_login"
down_revision = "0013_operator_session_idle"
branch_labels = None
depends_on = None

_TABLE = "operator_browser_session"
_COLUMNS = (
    ("operator_issuer", sa.Text()),
    ("operator_subject", sa.Text()),
    ("operator_username", sa.Text()),
    ("access_token", sa.Text()),
    ("access_token_expires_at", sa.DateTime(timezone=True)),
    ("refresh_token", sa.Text()),
)
_CHECKS = {
    "operator_browser_session_operator_whole": "(operator_issuer IS NULL) = (operator_subject IS NULL) "
    "AND (operator_issuer IS NULL) = (operator_username IS NULL)",
    "operator_browser_session_access_token_whole": "(access_token IS NULL) = (access_token_expires_at IS NULL)",
    "operator_browser_session_tokens_logged_in": "access_token IS NULL OR operator_issuer IS NOT NULL",
    "operator_browser_session_refresh_token_with_access_token": "refresh_token IS NULL OR access_token IS NOT NULL",
}


def upgrade() -> None:
    op.execute(f"DELETE FROM {_TABLE}")
    for name, type_ in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, type_, nullable=True))
    for name, condition in _CHECKS.items():
        op.create_check_constraint(name, _TABLE, condition)


def downgrade() -> None:
    op.execute(f"DELETE FROM {_TABLE}")
    for name in _CHECKS:
        op.drop_constraint(name, _TABLE, type_="check")
    for name, _ in _COLUMNS:
        op.drop_column(_TABLE, name)
