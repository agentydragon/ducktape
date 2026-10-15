"""Session RPC requests built from a caller's JSON launch overrides."""

from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.runner_gateway import wire
from agentplane.sandboxes.testing.kubernetes import DESTINATION


def test_wire_preserves_explicit_empty_fields_in_the_complete_spec() -> None:
    request = wire.create_proto(
        DESTINATION, uuid4(), {"model": "test-model", "instructions": "", "reasoningEffort": ""}, ""
    )
    assert request.spec.model == "test-model"
    assert request.spec.instructions == ""
    assert request.spec.reasoning_effort == ""
    assert request.HasField("setup_script")
    assert request.setup_script == ""


def test_duplicate_spec_aliases_are_refused() -> None:
    with pytest.raises(ValueError, match="both proto and JSON"):
        wire.create_proto(DESTINATION, uuid4(), {"reasoning_effort": "low", "reasoningEffort": "high"}, None)


if __name__ == "__main__":
    pytest_bazel.main()
