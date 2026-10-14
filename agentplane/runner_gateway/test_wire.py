"""Session RPC requests built from a caller's JSON launch overrides."""

from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.runner_gateway import wire
from agentplane.sandboxes.testing.kubernetes import DESTINATION


def test_wire_preserves_explicit_empty_overrides() -> None:
    request = wire.create_proto(
        DESTINATION, uuid4(), {"model": "test-model", "instructions": "", "reasoningEffort": ""}, ""
    )
    assert wire.launch_overrides(request) == {"model": "test-model", "instructions": "", "reasoningEffort": ""}
    assert request.HasField("setup_script")
    assert request.setup_script == ""
    request.override_mask.paths.append("unknown")
    with pytest.raises(ValueError, match="unique SessionSpec field names"):
        wire.launch_overrides(request)
    request.override_mask.paths[:] = ["model"]
    request.spec.cwd = "/unselected"
    with pytest.raises(ValueError, match="every supplied SessionSpec field"):
        wire.launch_overrides(request)


def test_duplicate_spec_aliases_are_refused() -> None:
    with pytest.raises(ValueError, match="both proto and JSON"):
        wire.create_proto(DESTINATION, uuid4(), {"reasoning_effort": "low", "reasoningEffort": "high"}, None)


if __name__ == "__main__":
    pytest_bazel.main()
