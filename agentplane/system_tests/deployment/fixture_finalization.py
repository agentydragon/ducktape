"""Finish a suite-owned chart as the bare-k3s fixture consumed by the system-test stack."""

from __future__ import annotations

from cdk8s import Chart, Testing as Cdk8sTesting
from constructs import Construct

from agentplane.system_tests.deployment import dex, fixture_chart
from agentplane.system_tests.deployment.rendered_fixture import (
    Backend,
    BrowserService,
    LocalModel,
    McpFixtures,
    Models,
    PublicLiteLLM,
    RenderedFixture,
    SandboxRuntime,
)
from cluster.cdk8s.agentplane.environment import Environment


def finalize_fixture(
    chart: Chart,
    env: Environment,
    models: Models,
    *,
    app: fixture_chart.AppFixture | None = None,
    central_sampler: Construct | None = None,
    mcp_fixtures: McpFixtures | None = None,
) -> RenderedFixture:
    """Synthesize one directly-authored suite chart and derive its stack metadata."""
    if (env.app is None) != (app is None):
        raise ValueError("the App construction must match the fixture environment")
    if (env.central_sampler is None) != (central_sampler is None):
        raise ValueError("the central sampler construction must match the fixture environment")
    if mcp_fixtures is not None and app is None:
        raise ValueError("MCP fixture access requires the integration App")

    runtime = _runtime(env)
    backend = _backend(models)
    objects = list(Cdk8sTesting.synth(chart))
    if env.vm_sandboxes:
        fixture_chart.configure_test_vm_guests(objects)
    fixture_chart.add_svid_box_template(objects)
    fixture_chart.configure_port_forward_tls(objects)
    # The retained test node is bare k3s, which has no Cilium controller.
    objects = [document for document in objects if document["kind"] != "CiliumNetworkPolicy"]

    browser_services = [app.browser_service] if app is not None else []
    if mcp_fixtures is not None:
        service = dex.service()
        if mcp_fixtures.dex_host != service.host:
            raise ValueError(f"{mcp_fixtures.dex_host=} does not match {service.host=}")
        browser_services.append(BrowserService(name=service.name, host=service.host, fqdn=service.fqdn))

    return RenderedFixture(
        backend=backend,
        runtime=runtime,
        central_sampler=central_sampler is not None,
        namespace=env.namespace,
        app_origin=app.origin if app is not None else None,
        browser_services=browser_services,
        operator_tokens=app.operator_tokens if app is not None else None,
        mcp_fixtures=mcp_fixtures,
        objects=objects,
    )


def _backend(models: Models) -> Backend:
    match models:
        case LocalModel():
            return Backend.LOCAL_MODEL
        case PublicLiteLLM():
            return Backend.PUBLIC_LITELLM
    raise TypeError(f"unsupported fixture model configuration: {type(models).__name__}")


def _runtime(env: Environment) -> SandboxRuntime:
    if env.vm_sandboxes and env.firecracker_sandboxes:
        raise ValueError("the fixture cannot use KubeVirt and Firecracker at the same time")
    if env.firecracker_sandboxes:
        return SandboxRuntime.FIRECRACKER
    if env.vm_sandboxes:
        return SandboxRuntime.KUBEVIRT
    return SandboxRuntime.POD
