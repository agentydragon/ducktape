"""Build the system-test deployment without external OAuth fixture servers."""

from dataclasses import asdict

from agentplane.system_tests.deployment import fixture_chart, fixture_database, fixture_finalization
from agentplane.system_tests.deployment.rendered_fixture import (
    MODEL_PLACEHOLDERS,
    OPERATOR_TOKEN_KEY_PLACEHOLDER,
    PLACEHOLDERS,
    Backend,
    RenderedFixture,
    SandboxRuntime,
)
from cluster.cdk8s.agentplane.service_database import Database


def render(
    *,
    runtime: SandboxRuntime,
    backend: Backend,
    include_app: bool,
    include_mcp_edge: bool = False,
    include_central_sampler: bool = False,
    guest_root_host_disk: bool = False,
) -> RenderedFixture:
    models = MODEL_PLACEHOLDERS[backend]
    if include_central_sampler:
        if backend is not Backend.LOCAL_MODEL:
            raise ValueError("central sampler fixtures require the scripted model backend")
        routes = fixture_chart.LOCAL_MODEL_ROUTES_WITH_SAMPLER
    elif backend is Backend.LOCAL_MODEL:
        routes = fixture_chart.LOCAL_MODEL_ROUTES
    else:
        routes = fixture_chart.PUBLIC_LITELLM_ROUTES

    operator_oidc = fixture_chart.operator_oidc()
    app_props = (
        fixture_chart.app_props(
            routes=routes,
            operator_oidc=operator_oidc,
            operator_token_key=OPERATOR_TOKEN_KEY_PLACEHOLDER,
            app_origin=fixture_chart.APP_ORIGIN,
        )
        if include_app
        else None
    )
    sampler_props = fixture_chart.central_sampler_props(routes.central_sampler) if include_central_sampler else None
    env = fixture_chart.environment(
        runtime=runtime,
        routes=routes,
        app=app_props,
        central_sampler=sampler_props,
        actions=fixture_chart.actions_props(
            operator_oidc=operator_oidc if include_app else None,
            oauth_secret_items=("client-secret",) if include_app else (),
            central_sampler_session_delegation=include_central_sampler,
        ),
        mcp_edge=fixture_chart.mcp_edge_props(operator_oidc=operator_oidc, oauth_secret_name=None),
    )

    chart = fixture_chart.new_chart(env)
    databases = list(fixture_chart.BACKEND_DATABASES)
    if include_app:
        databases.append(Database.APP)
    if include_mcp_edge:
        databases.append(Database.MCP_EDGE)
    if include_central_sampler:
        databases.append(Database.CENTRAL_SAMPLER)
    fixture_database.add_database(chart, namespace=env.namespace, databases=databases)
    fixture_chart.add_fixture_pki(chart, env, **asdict(PLACEHOLDERS))
    if not include_app:
        fixture_chart.add_native_test_client(chart, env)
    fixture_chart.add_backend_services(
        chart,
        env,
        models=models,
        platform_callers=fixture_chart.APP_CALLERS if include_app else fixture_chart.NATIVE_CALLERS,
        notification_callers=[fixture_chart.APP_SERVICE.pods] if include_app else [],
    )
    sampler = fixture_chart.add_central_sampler(chart, env) if include_central_sampler else None
    app = fixture_chart.add_app(chart, env) if include_app else None
    if include_mcp_edge:
        fixture_chart.add_mcp_edge(chart, env)

    runner = fixture_chart.add_runner_templates(chart, env)
    if runtime is SandboxRuntime.FIRECRACKER:
        fixture_chart.add_firecracker_template(chart, env, runner)
    elif runtime is SandboxRuntime.KUBEVIRT:
        fixture_chart.add_vm_templates(chart, env, runner, guest_root_host_disk=guest_root_host_disk)
    boxes = fixture_chart.add_command_templates(chart, env)
    fixture_chart.add_svid_box_template(boxes, env)
    fixture_chart.add_model_credentials(chart, env, models=models)
    fixture_chart.add_policy_controller(chart, env)
    return fixture_finalization.finalize_fixture(chart, env, models, app=app, central_sampler=sampler)
