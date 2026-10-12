"""Initialization correlates control replies without consuming background results."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.native.claude import facade, wire
from agentplane.native.testing.async_process import AsyncNativeProcess, NativeProcessEofError

# A pipe peer, not a mock model: it copies the actual request ID from the
# facade's outbound frame and deliberately interleaves unrelated traffic.
PEER = """
import json
import sys
request = json.loads(sys.stdin.readline())
frames = [
    {"type": "system", "subtype": "task_notification", "task_id": "child", "status": "stopped"},
    {"type": "result", "subtype": "success", "is_error": False, "result": "", "num_turns": 0,
     "origin": {"kind": "task-notification"}},
    {"type": "result", "subtype": "error_during_execution", "is_error": True},
    {"type": "control_response", "response": {"subtype": "success", "request_id": "another-request"}},
]
if sys.argv[1] != "eof":
    frames.append({"type": "control_response", "response": {
        "subtype": sys.argv[1], "request_id": request["request_id"], "error": "probe rejection"
    }})
for frame in frames:
    print(json.dumps(frame), flush=True)
"""


@pytest.mark.parametrize("outcome", ["success", "error", "eof"])
async def test_initialize_requires_its_control_reply(tmp_path: Path, outcome: str) -> None:
    async with AsyncNativeProcess(
        tmp_path, [sys.executable, "-c", PEER, outcome], cwd=tmp_path, environment=dict(os.environ)
    ) as process:
        observed = process.frames()
        harness = facade.ClaudeHarness(process)
        if outcome == "eof":
            with pytest.raises(NativeProcessEofError, match="stdout closed"):
                await harness.initialize()
        elif outcome == "error":
            with pytest.raises(RuntimeError, match="Claude initialization rejected: probe rejection"):
                await harness.initialize()
        else:
            receipt = await harness.initialize()
            assert isinstance(receipt.response, wire.ControlResponseFrame)
            assert receipt.response.response.subtype == "success"
            assert receipt.response.response.request_id != "another-request"
            assert receipt.sequence == 5
        # Matching does not consume the raw trace or another observer's cursor.
        assert (await observed.next())["subtype"] == "task_notification"
        result = await observed.next()
        assert result["type"] == "result"
        assert result["origin"] == {"kind": "task-notification"}
        assert (await observed.next())["is_error"] is True
        assert (await observed.next())["response"]["request_id"] == "another-request"


if __name__ == "__main__":
    pytest_bazel.main()
