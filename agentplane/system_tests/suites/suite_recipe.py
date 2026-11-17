"""Render a common suite with the deployment choices supplied by its Bazel target."""

import argparse

from agentplane.system_tests.deployment import fixture_chart, suite_deployment
from agentplane.system_tests.deployment.render_fixture import write_output
from agentplane.system_tests.deployment.rendered_fixture import MODEL_PLACEHOLDERS, Backend, SandboxRuntime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=SandboxRuntime, choices=list(SandboxRuntime))
    parser.add_argument("--backend", required=True, type=Backend, choices=list(Backend))
    parser.add_argument("--mcp-edge", required=True, choices=("include", "omit"))
    parser.add_argument("--template", required=True, choices=("pod", "firecracker", "kubevirt"))
    args = parser.parse_args()
    if args.runtime.value != args.template:
        parser.error("template must match the requested runtime")
    deployment = suite_deployment.begin_suite(
        runtime=args.runtime, backend=args.backend, include_mcp_edge=args.mcp_edge == "include"
    )
    if args.template == "firecracker":
        fixture_chart.add_firecracker_template(deployment.chart, deployment.env, deployment.runner_template)
    elif args.template == "kubevirt":
        fixture_chart.add_vm_templates(deployment.chart, deployment.env, deployment.runner_template)
    suite_deployment.add_common_postlude(deployment)
    fixture_chart.add_policy_controller(deployment.chart, deployment.env)
    write_output(suite_deployment.render_suite(deployment), models=MODEL_PLACEHOLDERS[args.backend])


if __name__ == "__main__":
    main()
