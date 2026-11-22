"""Build the MCP-only suites' deployment without the central sampler."""

from agentplane.system_tests.deployment import fixture_chart
from agentplane.system_tests.deployment.rendered_fixture import RenderedFixture
from agentplane.system_tests.suites.mcp.mcp_deployment import render_mcp_fixture


def render() -> RenderedFixture:
    return render_mcp_fixture(routes=fixture_chart.LOCAL_MODEL_ROUTES, central_sampler_session_delegation=False)
