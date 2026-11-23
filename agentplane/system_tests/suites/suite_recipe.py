"""Render all system-test suite fixtures in one Bazel action."""

import argparse
from collections.abc import Callable
from functools import partial
from pathlib import Path

from agentplane.system_tests.deployment import fixture_chart, suite_deployment
from agentplane.system_tests.deployment.render_fixture import write_fixture
from agentplane.system_tests.deployment.rendered_fixture import (
    MCP_PLACEHOLDERS,
    MODEL_PLACEHOLDERS,
    Backend,
    McpInputs,
    RenderedFixture,
    SandboxRuntime,
)
from agentplane.system_tests.suites import native_deployment
from agentplane.system_tests.suites.mcp import (
    history_deployment,
    mcp_deployment,
    mcp_services_deployment,
    recovery_deployment,
)


def render(path: Path, *, runtime: SandboxRuntime, backend: Backend, include_mcp_edge: bool) -> None:
    deployment = suite_deployment.begin_suite(runtime=runtime, backend=backend, include_mcp_edge=include_mcp_edge)
    if runtime == SandboxRuntime.KUBEVIRT:
        fixture_chart.add_vm_templates(deployment.chart, deployment.env, deployment.runner_template)
    suite_deployment.add_common_postlude(deployment)
    fixture_chart.add_policy_controller(deployment.chart, deployment.env)
    write_fixture(path, suite_deployment.render_suite(deployment), models=MODEL_PLACEHOLDERS[backend], mcp=None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    recipes = [
        ("pod_manifests.json", SandboxRuntime.POD, Backend.LOCAL_MODEL, False),
        ("probe_manifests.json", SandboxRuntime.POD, Backend.LOCAL_MODEL, True),
        ("vm_manifests.json", SandboxRuntime.KUBEVIRT, Backend.LOCAL_MODEL, False),
        ("real_model_manifests.json", SandboxRuntime.POD, Backend.PUBLIC_LITELLM, True),
    ]
    for filename, runtime, backend, include_mcp_edge in recipes:
        render(args.output_dir / filename, runtime=runtime, backend=backend, include_mcp_edge=include_mcp_edge)

    shared_model = MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL]
    renderers: list[tuple[str, Callable[[], RenderedFixture], McpInputs | None]] = [
        ("pod_native_manifests.json", partial(native_deployment.render, SandboxRuntime.POD), None),
        ("vm_native_manifests.json", partial(native_deployment.render, SandboxRuntime.KUBEVIRT), None),
        ("firecracker_manifests.json", partial(native_deployment.render, SandboxRuntime.FIRECRACKER), None),
        ("recovery_manifests.json", recovery_deployment.render, None),
        ("history_manifests.json", history_deployment.render, None),
        ("mcp_manifests.json", mcp_deployment.render, MCP_PLACEHOLDERS),
        ("mcp_services_manifests.json", mcp_services_deployment.render, MCP_PLACEHOLDERS),
    ]
    for filename, recipe, mcp in renderers:
        write_fixture(args.output_dir / filename, recipe(), models=shared_model, mcp=mcp)


if __name__ == "__main__":
    main()
