"""Pydantic models for Claude Code's local credentials file (~/.claude/.credentials.json)."""

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


class OAuthCredentials(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None
    scopes: list[str] | None = None
    subscription_type: str | None = None
    rate_limit_tier: str | None = None


class Credentials(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    claude_ai_oauth: OAuthCredentials | None = None


def read_credentials() -> OAuthCredentials | None:
    """Read OAuth credentials from Claude credentials file."""
    try:
        creds = Credentials.model_validate_json(CREDENTIALS_PATH.read_text())
        return creds.claude_ai_oauth
    except OSError, ValueError:
        logger.debug("Could not read Claude credentials from %s", CREDENTIALS_PATH)
        return None
