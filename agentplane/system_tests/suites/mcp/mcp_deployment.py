"""Compose the MCP acceptance suites' shared deployment recipe."""

from dataclasses import asdict

from agentplane.system_tests.deployment import fixture_chart, fixture_database, mcp_fixture_chart
from agentplane.system_tests.deployment.fixture_finalization import finalize_fixture
from agentplane.system_tests.deployment.rendered_fixture import (
    MCP_PLACEHOLDERS,
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    Backend,
    McpFixtures,
    RenderedFixture,
    SandboxRuntime,
)
from cluster.cdk8s.agentplane.service_database import Database


def render_mcp_fixture(
    *, routes: fixture_chart.HarnessRoutes, central_sampler_session_delegation: bool
) -> RenderedFixture:
    models = MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL]
    operator_oidc = fixture_chart.operator_oidc()
    app_props = fixture_chart.app_props(
        routes=routes, operator_oidc=operator_oidc, operator_token_key=None, app_origin=MCP_PLACEHOLDERS.app_origin
    )
    actions_props = fixture_chart.actions_props(
        operator_oidc=operator_oidc,
        oauth_secret_items=("client-secret",),
        central_sampler_session_delegation=central_sampler_session_delegation,
    )
    actions_props = mcp_fixture_chart.mcp_actions_props(actions_props, MCP_PLACEHOLDERS)
    central_sampler = fixture_chart.central_sampler_props(routes.central_sampler) if routes.central_sampler else None
    env = fixture_chart.environment(
        runtime=SandboxRuntime.POD,
        routes=routes,
        app=app_props,
        central_sampler=central_sampler,
        actions=actions_props,
        mcp_edge=fixture_chart.mcp_edge_props(
            operator_oidc=operator_oidc, oauth_secret_name=mcp_fixture_chart.MCP_EDGE_OAUTH_SECRET
        ),
    )
    chart = fixture_chart.new_chart(env)
    fixture_database.add_database(
        chart,
        namespace=env.namespace,
        databases=(
            *fixture_chart.BACKEND_DATABASES,
            Database.APP,
            Database.MCP_EDGE,
            *((Database.CENTRAL_SAMPLER,) if central_sampler is not None else ()),
        ),
    )
    fixture_chart.add_fixture_pki(chart, env, **asdict(PLACEHOLDERS))
    actions = fixture_chart.add_backend_services(
        chart,
        env,
        models=models,
        platform_callers=fixture_chart.APP_CALLERS,
        notification_callers=[fixture_chart.APP_SERVICE.pods],
    )
    sampler = fixture_chart.add_central_sampler(chart, env) if central_sampler is not None else None
    app = fixture_chart.add_app(chart, env)
    edge = fixture_chart.add_mcp_edge(chart, env)
    fixture_chart.add_runner_templates(chart, env)
    boxes = fixture_chart.add_command_templates(chart, env)
    fixture_chart.add_svid_box_template(boxes, env)
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
    return finalize_fixture(chart, env, models, app=app, central_sampler=sampler, mcp_fixtures=mcp_fixtures)


def render() -> RenderedFixture:
    return render_mcp_fixture(
        routes=fixture_chart.LOCAL_MODEL_ROUTES_WITH_SAMPLER, central_sampler_session_delegation=True
    )
