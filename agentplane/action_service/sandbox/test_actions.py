from __future__ import annotations

import jsonschema
import pytest
import pytest_bazel

from agentplane.action_service.sandbox.actions import SandboxAction, actions
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding

# types-jsonschema stubs import referencing; the mypy aspect needs that typed package directly.
# gazelle:include_dep @pypi//referencing

NAMESPACE = "agentplane-test"
TEMPLATE = "test-template"
BINDING = SandboxExecutorBinding(description="test sandboxes", namespace=NAMESPACE, templates={TEMPLATE})


def test_the_offered_actions_name_the_offered_templates() -> None:
    """create's own description is where an agent learns which templates exist, and naming one that
    is not offered is the likeliest way to get create wrong."""
    offered = actions(BINDING, {TEMPLATE: "the test box"})
    assert set(offered) == set(SandboxAction)
    assert '"test-template": "the test box"' in offered[SandboxAction.CREATE].description
    assert offered[SandboxAction.EXEC].input_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("timeout_seconds", "max_output_bytes", "admitted"), [(60, 1000, True), (61, 1000, False), (60, 1001, False)]
)
def test_exec_admits_nothing_past_the_deployment_caps(
    timeout_seconds: int, max_output_bytes: int, admitted: bool
) -> None:
    """Submission checks arguments against the advertised schema, as the service does. Past a cap,
    exec would cut the run short, so the request is refused where the caller sees why."""
    binding = SandboxExecutorBinding(
        description="test sandboxes",
        namespace=NAMESPACE,
        templates={TEMPLATE},
        max_timeout_seconds=60,
        max_output_bytes=1000,
    )
    schema = actions(binding, {TEMPLATE: "the test box"})[SandboxAction.EXEC].input_schema
    arguments = {
        "name": "box",
        "script": "true",
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
    }
    if admitted:
        jsonschema.validate(arguments, schema)
    else:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(arguments, schema)


if __name__ == "__main__":
    pytest_bazel.main()
