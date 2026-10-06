"""Register Action Service test fixtures."""

from agentplane.action_service.testing.fixtures import (
    db_url,
    echo_catalog,
    echo_executor,
    echo_mcp_url,
    engine,
    execution_lease,
    github_visibility,
    mcp_executor,
    scripted,
)
from util.testing.postgres_fixtures import postgres_container
