"""Build the MCP-only suites' deployment from explicit cdk8s additions."""

from dataclasses import asdict

from agentplane.system_tests.deployment import fixture_chart, fixture_database, mcp_fixture_chart
from agentplane.system_tests.deployment.fixture_finalization import finalize_fixture
from agentplane.system_tests.deployment.render_fixture import write_output
from agentplane.system_tests.deployment.rendered_fixture import (
    MCP_PLACEHOLDERS,
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    Backend,
    McpFixtures,
    RenderedFixture,
    SandboxRuntime,
)


def render() -> RenderedFixture:
    models = MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL]
    env = fixture_chart.environment(
        runtime=SandboxRuntime.POD,
        platform_callers=fixture_chart.APP_CALLERS,
        models=models,
        operator_token_key=None,
        app_enabled=True,
        central_sampler=False,
        app_origin=MCP_PLACEHOLDERS.app_origin,
    )
    env = mcp_fixture_chart.configure_environment(env, MCP_PLACEHOLDERS)
    chart = fixture_chart.new_chart(env)
    fixture_database.add_database(
        chart,
        namespace=env.namespace,
        databases=(*fixture_chart.BACKEND_DATABASES, fixture_database.Database.APP, fixture_database.Database.MCP_EDGE),
    )
    fixture_chart.add_fixture_pki(chart, env, **asdict(PLACEHOLDERS))
    actions = fixture_chart.add_backend_services(
        chart,
        env,
        models=models,
        platform_callers=fixture_chart.APP_CALLERS,
        notification_callers=[fixture_chart.APP_SERVICE.pods],
    )
    app = fixture_chart.add_app(chart, env)
    edge = fixture_chart.add_mcp_edge(chart, env)
    fixture_chart.add_sandbox_templates(chart, env)
    fixture_chart.add_model_credentials(chart, env, models=models)
    fixture_chart.add_policy_controller(chart, env)
    mcp_fixture_chart.add_mcp_fixtures(
        chart, MCP_PLACEHOLDERS, system_roots=PLACEHOLDERS.system_roots, app=app, actions=actions, edge=edge
    )
    mcp_fixtures = McpFixtures(
        dex_host=fixture_chart.DEX_HOST,
        operator_secret=mcp_fixture_chart.OPERATOR_SECRET,
        operator_token_grant=mcp_fixture_chart.OPERATOR_TOKEN_GRANT,
        mcp_edge_service=fixture_chart.MCP_EDGE_HTTP_SERVICE.name,
        mcp_edge_port=fixture_chart.MCP_EDGE_HTTP_SERVICE.port.number,
        app_service=app.service.name,
        app_port=app.service.port.number,
    )
    return finalize_fixture(chart, env, models, app=app, mcp_fixtures=mcp_fixtures)


if __name__ == "__main__":
    write_output(render(), models=MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL], mcp=MCP_PLACEHOLDERS)
