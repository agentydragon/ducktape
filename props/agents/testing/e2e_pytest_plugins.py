"""Pytest fixture plugins shared by the agent container E2E tests."""

# gazelle:include_dep //testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
# gazelle:include_dep //props/testing/fixtures:runs
# gazelle:include_dep //props/testing/fixtures:scopes
# gazelle:include_dep //mcp_infra/testing:fixtures
# gazelle:include_dep //props/testing/fixtures:e2e_infra
# gazelle:include_dep //props/testing/fixtures:e2e_container
pytest_plugins = (
    "testing.postgres_fixtures",
    "props.testing.fixtures.db",
    "props.testing.fixtures.runs",
    "props.testing.fixtures.scopes",
    "mcp_infra.testing.fixtures",
    "props.testing.fixtures.e2e_infra",
    "props.testing.fixtures.e2e_container",
)
