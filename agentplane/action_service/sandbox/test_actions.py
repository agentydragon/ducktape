from __future__ import annotations

import pytest_bazel

from agentplane.action_service.sandbox.actions import SandboxAction, actions
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding

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


if __name__ == "__main__":
    pytest_bazel.main()
