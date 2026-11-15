"""Build the firecracker suite's deployment from explicit cdk8s additions."""

from dataclasses import asdict

from cdk8s import Testing

from agentplane.system_tests.deployment import fixture_chart, fixture_database
from agentplane.system_tests.deployment.render_fixture import write_output
from agentplane.system_tests.deployment.rendered_fixture import (
    MODEL_PLACEHOLDERS,
    OPERATOR_TOKEN_KEY_PLACEHOLDER,
    PLACEHOLDERS,
    Backend,
    BrowserService,
    RenderedFixture,
    SandboxRuntime,
)


def render() -> RenderedFixture:
    models = MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL]
    env = fixture_chart.environment(
        runtime=SandboxRuntime.FIRECRACKER,
        platform_callers=fixture_chart.APP_CALLERS,
        models=models,
        operator_token_key=OPERATOR_TOKEN_KEY_PLACEHOLDER,
        app_enabled=True,
        central_sampler=False,
    )
    chart = fixture_chart.new_chart(env)
    fixture_database.add_database(
        chart, namespace=env.namespace, databases=(*fixture_chart.BACKEND_DATABASES, fixture_database.Database.APP)
    )
    fixture_chart.add_fixture_pki(chart, env, **asdict(PLACEHOLDERS))
    fixture_chart.add_backend_services(
        chart,
        env,
        models=models,
        platform_callers=fixture_chart.APP_CALLERS,
        notification_callers=[fixture_chart.APP_SERVICE.pods],
    )
    fixture_chart.add_app(chart, env)
    fixture_chart.add_sandbox_templates(chart, env)
    fixture_chart.add_model_credentials(chart, env, models=models)
    fixture_chart.add_policy_controller(chart, env)
    documents = list(Testing.synth(chart))
    fixture_chart.add_svid_box_template(documents)
    fixture_chart.configure_port_forward_tls(documents)
    # Bare k3s has no Cilium controller.
    documents = [doc for doc in documents if doc["kind"] != "CiliumNetworkPolicy"]
    return RenderedFixture(
        backend=Backend.LOCAL_MODEL,
        runtime=SandboxRuntime.FIRECRACKER,
        central_sampler=False,
        namespace=env.namespace,
        app_origin=fixture_chart.APP_ORIGIN,
        browser_services=[
            BrowserService(
                name=fixture_chart.APP_SERVICE.name,
                host=fixture_chart.APP_SERVICE.host,
                fqdn=fixture_chart.APP_SERVICE.fqdn,
            )
        ],
        operator_tokens=fixture_chart.OPERATOR_TOKENS,
        mcp_fixtures=None,
        objects=documents,
    )


if __name__ == "__main__":
    write_output(render(), models=MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL])
