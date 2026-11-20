"""Build the native Pod platform suite's deployment from explicit cdk8s additions."""

from dataclasses import asdict

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
    operator_oidc = fixture_chart.operator_oidc()
    env = fixture_chart.environment(
        runtime=SandboxRuntime.POD,
        routes=fixture_chart.LOCAL_MODEL_ROUTES,
        app=None,
        central_sampler=None,
        actions=fixture_chart.actions_props(
            operator_oidc=None, oauth_secret_items=(), central_sampler_session_delegation=False
        ),
        mcp_edge=fixture_chart.mcp_edge_props(operator_oidc=operator_oidc, oauth_secret_name=None),
    )
    chart = fixture_chart.new_chart(env)
    fixture_database.add_database(chart, namespace=env.namespace, databases=fixture_chart.BACKEND_DATABASES)
    fixture_chart.add_fixture_pki(chart, env, **asdict(PLACEHOLDERS))
    fixture_chart.add_native_test_client(chart, env)
    fixture_chart.add_backend_services(
        chart, env, models=models, platform_callers=fixture_chart.NATIVE_CALLERS, notification_callers=[]
    )
    fixture_chart.add_runner_templates(chart, env)
    boxes = fixture_chart.add_command_templates(chart, env)
    fixture_chart.add_svid_box_template(boxes, env)
    fixture_chart.add_model_credentials(chart, env, models=models)
    fixture_chart.add_policy_controller(chart, env)
    return finalize_fixture(chart, env, models)
