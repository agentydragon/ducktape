"""Build the recovery suite's deployment from explicit cdk8s additions."""

from dataclasses import asdict, replace

from agentplane.system_tests.deployment import fixture_chart, fixture_database
from agentplane.system_tests.deployment.fixture_finalization import finalize_fixture
from agentplane.system_tests.deployment.rendered_fixture import (
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    Backend,
    RenderedFixture,
    SandboxRuntime,
)


def render() -> RenderedFixture:
    models = MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL]
    routes = replace(fixture_chart.LOCAL_MODEL_ROUTES, central_sampler=(fixture_chart.GPT6_LUNA_RESPONSES,))
    operator_oidc = fixture_chart.operator_oidc()
    central_sampler_props = fixture_chart.central_sampler_props(model=fixture_chart.GPT6_LUNA_RESPONSES)
    actions_props = fixture_chart.actions_props(
        operator_oidc=None, oauth_secret_items=(), central_sampler_session_delegation=True
    )
    env = fixture_chart.environment(
        runtime=SandboxRuntime.POD,
        routes=routes,
        app=None,
        central_sampler=central_sampler_props,
        actions=actions_props,
        mcp_edge=fixture_chart.mcp_edge_props(operator_oidc=operator_oidc, oauth_secret_name=None),
    )
    chart = fixture_chart.new_chart(env)
    fixture_database.add_database(
        chart,
        namespace=env.namespace,
        databases=(*fixture_chart.BACKEND_DATABASES, fixture_database.Database.CENTRAL_SAMPLER),
    )
    fixture_chart.add_fixture_pki(chart, env, **asdict(PLACEHOLDERS))
    fixture_chart.add_native_test_client(chart, env)
    fixture_chart.add_backend_services(
        chart, env, models=models, platform_callers=fixture_chart.NATIVE_CALLERS, notification_callers=[]
    )
    central_sampler = fixture_chart.add_central_sampler(chart, env)
    fixture_chart.add_runner_templates(chart, env)
    boxes = fixture_chart.add_command_templates(chart, env)
    fixture_chart.add_svid_box_template(boxes, env)
    fixture_chart.add_model_credentials(chart, env, models=models)
    fixture_chart.add_policy_controller(chart, env)
    return finalize_fixture(chart, env, models, central_sampler=central_sampler)
