"""Render the common system-test suite fixtures."""

import argparse
from pathlib import Path

from agentplane.system_tests.deployment import fixture_chart, suite_deployment
from agentplane.system_tests.deployment.render_fixture import write_fixture
from agentplane.system_tests.deployment.rendered_fixture import MODEL_PLACEHOLDERS, Backend, SandboxRuntime


def render(path: Path, *, runtime: SandboxRuntime, backend: Backend, include_mcp_edge: bool) -> None:
    deployment = suite_deployment.begin_suite(runtime=runtime, backend=backend, include_mcp_edge=include_mcp_edge)
    if runtime == SandboxRuntime.FIRECRACKER:
        fixture_chart.add_firecracker_template(deployment.chart, deployment.env, deployment.runner_template)
    elif runtime == SandboxRuntime.KUBEVIRT:
        fixture_chart.add_vm_templates(deployment.chart, deployment.env, deployment.runner_template)
    suite_deployment.add_common_postlude(deployment)
    fixture_chart.add_policy_controller(deployment.chart, deployment.env)
    write_fixture(path, suite_deployment.render_suite(deployment), models=MODEL_PLACEHOLDERS[backend], mcp=None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    render(
        args.output_dir / "pod_manifests.json",
        runtime=SandboxRuntime.POD,
        backend=Backend.LOCAL_MODEL,
        include_mcp_edge=False,
    )
    render(
        args.output_dir / "probe_manifests.json",
        runtime=SandboxRuntime.POD,
        backend=Backend.LOCAL_MODEL,
        include_mcp_edge=True,
    )
    render(
        args.output_dir / "vm_manifests.json",
        runtime=SandboxRuntime.KUBEVIRT,
        backend=Backend.LOCAL_MODEL,
        include_mcp_edge=False,
    )
    render(
        args.output_dir / "firecracker_manifests.json",
        runtime=SandboxRuntime.FIRECRACKER,
        backend=Backend.LOCAL_MODEL,
        include_mcp_edge=False,
    )
    render(
        args.output_dir / "real_model_manifests.json",
        runtime=SandboxRuntime.POD,
        backend=Backend.PUBLIC_LITELLM,
        include_mcp_edge=True,
    )


if __name__ == "__main__":
    main()
