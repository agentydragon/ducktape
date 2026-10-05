"""`Settings` construction: the deploy-config file source and the process-config cross-field checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import SecretStr, ValidationError

from haku.console.config import McpOAuthConfig
from haku.console.conftest import console_settings
from mcp_infra.persistence import PostgresPersistence

# `console_settings` only stores the database URL, so these tests need no database.
_CONSOLE_DATABASE_URL = "postgresql+psycopg://app:secret@db.example.test:5432/haku"


def test_mcp_oauth_persistence_must_share_the_console_database() -> None:
    oauth = McpOAuthConfig(
        oidc_issuer="https://auth.example.test/application/o/haku-console-mcp/",
        oidc_client_id="console",
        oidc_client_secret=SecretStr("secret"),
        persistence=PostgresPersistence(kind="postgres", url="postgresql://app:secret@other-db.example.test:5432/haku"),
    )

    with pytest.raises(ValidationError):
        console_settings(_CONSOLE_DATABASE_URL, mcp_oauth=oauth)


def test_missing_deploy_config_fails_startup() -> None:
    with pytest.raises(RuntimeError, match=re.escape("/nonexistent/haku-console.yaml")):
        console_settings(_CONSOLE_DATABASE_URL, config_file=Path("/nonexistent/haku-console.yaml"))


if __name__ == "__main__":
    pytest_bazel.main()
