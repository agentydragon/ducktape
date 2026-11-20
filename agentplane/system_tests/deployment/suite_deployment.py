"""Shared construction steps for the direct cdk8s system-test deployments."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from cdk8s import Chart

from agentplane.system_tests.deployment import fixture_chart, fixture_database, fixture_finalization
from agentplane.system_tests.deployment.rendered_fixture import (
    MODEL_PLACEHOLDERS,
    OPERATOR_TOKEN_KEY_PLACEHOLDER,
    PLACEHOLDERS,
    Backend,
    Models,
    RenderedFixture,
    SandboxRuntime,
)
from cluster.cdk8s.agentplane import environment_sandbox
from cluster.cdk8s.agentplane.environment import Environment


@dataclass(frozen=True)
class SuiteDeployment:
    chart: Chart
    env: Environment
    models: Models
    app: fixture_chart.AppFixture
    runner_template: environment_sandbox.EnvironmentSandboxTemplate


def begin_suite(*, runtime: SandboxRuntime, backend: Backend, include_mcp_edge: bool) -> SuiteDeployment:
    """Build the shared database, PKI, service, app, and runner baseline."""
    models = MODEL_PLACEHOLDERS[backend]
    routes = {
        Backend.LOCAL_MODEL: fixture_chart.LOCAL_MODEL_ROUTES,
        Backend.PUBLIC_LITELLM: fixture_chart.PUBLIC_LITELLM_ROUTES,
    }[backend]
    operator_oidc = fixture_chart.operator_oidc()
    app_props = fixture_chart.app_props(
        routes=routes,
        operator_oidc=operator_oidc,
        operator_token_key=OPERATOR_TOKEN_KEY_PLACEHOLDER,
        app_origin=fixture_chart.APP_ORIGIN,
    )
    actions_props = fixture_chart.actions_props(
        operator_oidc=operator_oidc, oauth_secret_items=("client-secret",), central_sampler_session_delegation=False
    )
    env = fixture_chart.environment(
        runtime=runtime,
        routes=routes,
        app=app_props,
        central_sampler=None,
        actions=actions_props,
        mcp_edge=fixture_chart.mcp_edge_props(operator_oidc=operator_oidc, oauth_secret_name=None),
    )
    chart = fixture_chart.new_chart(env)
    databases: tuple[fixture_database.Database, ...] = (*fixture_chart.BACKEND_DATABASES, fixture_database.Database.APP)
    if include_mcp_edge:
        databases += (fixture_database.Database.MCP_EDGE,)
    fixture_database.add_database(chart, namespace=env.namespace, databases=databases)
    fixture_chart.add_fixture_pki(chart, env, **asdict(PLACEHOLDERS))
    fixture_chart.add_backend_services(
        chart,
        env,
        models=models,
        platform_callers=fixture_chart.APP_CALLERS,
        notification_callers=[fixture_chart.APP_SERVICE.pods],
    )
    app = fixture_chart.add_app(chart, env)
    if include_mcp_edge:
        fixture_chart.add_mcp_edge(chart, env)
    runner_template = fixture_chart.add_runner_templates(chart, env)
    return SuiteDeployment(chart, env, models, app, runner_template)


def add_common_postlude(deployment: SuiteDeployment) -> None:
    """Add command templates and model credentials after suite-specific templates."""
    boxes = fixture_chart.add_command_templates(deployment.chart, deployment.env)
    fixture_chart.add_svid_box_template(boxes, deployment.env)
    fixture_chart.add_model_credentials(deployment.chart, deployment.env, models=deployment.models)


def render_suite(deployment: SuiteDeployment) -> RenderedFixture:
    """Synthesize the completed suite chart and its stack metadata."""
    return fixture_finalization.finalize_fixture(
        deployment.chart, deployment.env, deployment.models, app=deployment.app
    )
