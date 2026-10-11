"""Operational prompt construction without the integration app or its database."""

import pytest_bazel

from agentplane.runner import protocol_pb2
from agentplane.runner_gateway.instructions import combine_instructions
from agentplane.runner_gateway.session_lifecycle import launch_spec
from agentplane.sandbox_service.protocol_pb2 import (
    SandboxBinding,
    SandboxDestination,
    ServiceAccount,
    SessionDefaults,
    SessionDestination,
)

# gazelle:include_dep @pypi//protobuf


def test_task_instructions_augment_platform_guidance() -> None:
    assert combine_instructions("  Platform  ", "  Task  ") == "Platform\n\nTask"
    assert combine_instructions("", "Task") == "Task"
    assert combine_instructions("Platform", "") == "Platform"


def _launched_instructions(overrides: dict[str, object]) -> str:
    return launch_spec(
        SessionDestination(
            sandbox=SandboxDestination(
                owner=ServiceAccount(namespace="test-namespace", name="test-owner"),
                sandbox="test-sandbox",
                sandbox_uid="test-sandbox-uid",
            ),
            session_id="test-session",
        ),
        overrides,
        binding=SandboxBinding(
            session_defaults=SessionDefaults(
                harness=protocol_pb2.HARNESS_CODEX, model="test-model", cwd="/test", instructions="Test preset task."
            )
        ),
        platform_instructions="Test platform guidance.",
    ).instructions


def test_launch_orders_platform_guidance_then_destination_then_task() -> None:
    instructions = _launched_instructions({})

    assert instructions.startswith("Test platform guidance.")
    assert instructions.index("test-session") < instructions.index("Test preset task.")
    assert instructions.endswith("Test preset task.")


def test_clearing_the_task_keeps_platform_guidance() -> None:
    instructions = _launched_instructions({"instructions": ""})

    assert instructions.startswith("Test platform guidance.")
    assert "Test preset task." not in instructions


if __name__ == "__main__":
    pytest_bazel.main()
