"""Initialization correlates control replies without consuming background results."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from agentplane.native.claude.testing.async_run import ClaudeRun
from agentplane.native.testing.async_process import NativeProcessEofError


@pytest.mark.parametrize("outcome", ["success", "error", "eof"])
async def test_initialize_requires_its_control_reply(runfiles: Runfiles, tmp_path: Path, outcome: str) -> None:
    peer = runfiles.root() / "ducktape/agentplane/native/claude/testing/control_reply_peer.py"
    async with ClaudeRun(
        tmp_path, [sys.executable, str(peer), outcome], cwd=tmp_path, environment=os.environ, initialize=False
    ) as run:
        observed = run.frames()
        if outcome == "eof":
            with pytest.raises(NativeProcessEofError, match="stdout closed"):
                await run.initialize()
        elif outcome == "error":
            with pytest.raises(RuntimeError, match="Claude initialization rejected: probe rejection"):
                await run.initialize()
        else:
            response = await run.initialize()
            assert response.response.subtype == "success"
            assert response.response.request_id != "another-request"
        # Matching does not consume the raw trace or another observer's cursor.
        assert (await observed.next())["subtype"] == "task_notification"
        result = await observed.next()
        assert result["type"] == "result"
        assert result["origin"] == {"kind": "task-notification"}
        assert (await observed.next())["is_error"] is True
        assert (await observed.next())["response"]["request_id"] == "another-request"


if __name__ == "__main__":
    pytest_bazel.main()
