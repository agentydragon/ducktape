"""Tests for the CLI's automatic compaction handler."""

from __future__ import annotations

import pytest_bazel

from agent_core.events import GroundTruthUsage, Response
from agent_core.loop_control import Compact, NoAction
from x.agent_cli.compaction import CompactionHandler


def test_compaction_handler_triggers_at_threshold():
    """Track tokens and request compaction after the threshold is exceeded."""
    handler = CompactionHandler(threshold_tokens=1000, keep_recent_turns=2)

    handler.on_response(
        Response(
            response_id="test-id", usage=GroundTruthUsage(model="gpt-4o-mini", total_tokens=500), model="gpt-4o-mini"
        )
    )
    assert isinstance(handler.on_before_sample(), NoAction)

    handler.on_response(
        Response(
            response_id="test-id2", usage=GroundTruthUsage(model="gpt-4o-mini", total_tokens=600), model="gpt-4o-mini"
        )
    )
    decision = handler.on_before_sample()
    assert isinstance(decision, Compact)
    assert decision.keep_recent_turns == 2

    handler.on_compaction_complete(compacted=True)
    assert isinstance(handler.on_before_sample(), NoAction)


if __name__ == "__main__":
    pytest_bazel.main()
