"""Register Action Service test fixtures."""

from agentplane.action_service.testing.fixtures import (
    action_database_template,
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
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
