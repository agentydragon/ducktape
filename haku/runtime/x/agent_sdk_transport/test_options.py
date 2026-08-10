"""Tests for Agent SDK conversation options."""

import pytest_bazel
from claude_agent_sdk import ClaudeAgentOptions

from haku.runtime.x.agent_sdk_transport.options import enable_fine_grained_streaming
from haku.runtime.x.agent_sdk_transport.protocol import FINE_GRAINED_TOOL_STREAMING_ENV


def test_enable_fine_grained_streaming_sets_both_sdk_controls() -> None:
    original = ClaudeAgentOptions(env={"EXISTING": "value"})

    configured = enable_fine_grained_streaming(original)

    assert configured.include_partial_messages is True
    assert configured.env == {"EXISTING": "value", FINE_GRAINED_TOOL_STREAMING_ENV: "1"}
    assert original.include_partial_messages is False
    assert original.env == {"EXISTING": "value"}


if __name__ == "__main__":
    pytest_bazel.main()
