"""Render all system-test suite fixtures in one Bazel action."""

import argparse
from pathlib import Path

from agentplane.system_tests.deployment import suite_deployment
from agentplane.system_tests.deployment.render_fixture import write_fixture
from agentplane.system_tests.deployment.rendered_fixture import (
    MCP_PLACEHOLDERS,
    MODEL_PLACEHOLDERS,
    Backend,
    SandboxRuntime,
)
from agentplane.system_tests.suites.mcp import mcp_deployment, mcp_services_deployment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    def render(
        filename: str,
        *,
        runtime: SandboxRuntime = SandboxRuntime.POD,
        backend: Backend = Backend.LOCAL_MODEL,
        include_app: bool,
        include_mcp_edge: bool = False,
        include_central_sampler: bool = False,
    ) -> None:
        fixture = suite_deployment.render(
            runtime=runtime,
            backend=backend,
            include_app=include_app,
            include_mcp_edge=include_mcp_edge,
            include_central_sampler=include_central_sampler,
        )
        write_fixture(args.output_dir / filename, fixture, models=MODEL_PLACEHOLDERS[backend], mcp=None)

    render("pod_manifests.json", include_app=True)
    render("probe_manifests.json", include_app=True, include_mcp_edge=True)
    render("vm_manifests.json", runtime=SandboxRuntime.KUBEVIRT, include_app=True)
    render("real_model_manifests.json", backend=Backend.PUBLIC_LITELLM, include_app=True, include_mcp_edge=True)
    render("pod_native_manifests.json", include_app=False)
    render("vm_native_manifests.json", runtime=SandboxRuntime.KUBEVIRT, include_app=False)
    render("firecracker_manifests.json", runtime=SandboxRuntime.FIRECRACKER, include_app=False)
    render("recovery_manifests.json", include_app=False, include_central_sampler=True)
    render("history_manifests.json", include_app=True, include_mcp_edge=True, include_central_sampler=True)

    for filename, recipe in (
        ("mcp_manifests.json", mcp_deployment.render),
        ("mcp_services_manifests.json", mcp_services_deployment.render),
    ):
        write_fixture(
            args.output_dir / filename, recipe(), models=MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL], mcp=MCP_PLACEHOLDERS
        )


if __name__ == "__main__":
    main()
