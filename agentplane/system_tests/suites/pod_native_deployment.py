"""Build the native Pod platform suite's deployment from explicit cdk8s additions."""

from dataclasses import asdict

from agentplane.system_tests.deployment import fixture_chart, fixture_database
from agentplane.system_tests.deployment.fixture_finalization import finalize_fixture
from agentplane.system_tests.deployment.render_fixture import write_output
from agentplane.system_tests.deployment.rendered_fixture import (
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    Backend,
    RenderedFixture,
    SandboxRuntime,
)


def render() -> RenderedFixture:
    models = MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL]
    env = fixture_chart.environment(
        runtime=SandboxRuntime.POD,
        platform_callers=fixture_chart.NATIVE_CALLERS,
        models=models,
        operator_token_key=None,
        app_enabled=False,
        central_sampler=False,
    )
    chart = fixture_chart.new_chart(env)
    fixture_database.add_database(chart, namespace=env.namespace, databases=fixture_chart.BACKEND_DATABASES)
    fixture_chart.add_fixture_pki(chart, env, **asdict(PLACEHOLDERS))
    fixture_chart.add_native_test_client(chart)
    fixture_chart.add_backend_services(
        chart, env, models=models, platform_callers=fixture_chart.NATIVE_CALLERS, notification_callers=[]
    )
    fixture_chart.add_sandbox_templates(chart, env)
    fixture_chart.add_model_credentials(chart, env, models=models)
    fixture_chart.add_policy_controller(chart, env)
    return finalize_fixture(chart, env, models)


if __name__ == "__main__":
    write_output(render(), models=MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL])
