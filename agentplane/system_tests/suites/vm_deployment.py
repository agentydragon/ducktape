"""Build the vm suite's deployment from explicit cdk8s additions."""

from agentplane.system_tests.deployment import fixture_chart, suite_deployment
from agentplane.system_tests.deployment.render_fixture import write_output
from agentplane.system_tests.deployment.rendered_fixture import (
    MODEL_PLACEHOLDERS,
    Backend,
    RenderedFixture,
    SandboxRuntime,
)


def render() -> RenderedFixture:
    deployment = suite_deployment.begin_suite(
        runtime=SandboxRuntime.KUBEVIRT, backend=Backend.LOCAL_MODEL, include_mcp_edge=False
    )
    fixture_chart.add_vm_templates(deployment.chart, deployment.env, deployment.runner_template)
    suite_deployment.add_common_postlude(deployment)
    fixture_chart.add_policy_controller(deployment.chart, deployment.env)
    return suite_deployment.render_suite(deployment)


if __name__ == "__main__":
    write_output(render(), models=MODEL_PLACEHOLDERS[Backend.LOCAL_MODEL])
