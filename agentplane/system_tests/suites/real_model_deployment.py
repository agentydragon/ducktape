"""Build the real model suite's deployment from explicit cdk8s additions."""

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
        runtime=SandboxRuntime.POD, backend=Backend.PUBLIC_LITELLM, include_mcp_edge=True
    )
    suite_deployment.add_common_postlude(deployment)
    fixture_chart.add_policy_controller(deployment.chart, deployment.env)
    return suite_deployment.render_suite(deployment)


if __name__ == "__main__":
    write_output(render(), models=MODEL_PLACEHOLDERS[Backend.PUBLIC_LITELLM])
