"""PreToolUse hook behavior: a deny blocks the tool and the model sees why; an allow lets it run.

Codex spawns the hook as its own child process rather than asking the driver over the wire, so
`capture/codex_hook.py` (already proven against a real endpoint by the `hooks`/`hooks_deny`
captures) is reused verbatim as that child: it logs every raw hook payload it receives to a file
this test reads back, which is the ground truth for what Codex actually sent it.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.native.capture import codex_hook
from agentplane.native.codex import scenarios, wire
from agentplane.native.codex.testing import frames, responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses

PROBE = "printf 'PROBE_OK\\n'"


def _hooks_config(log_path: Path, decision: str) -> dict[str, Any]:
    command = shlex.join([sys.executable, codex_hook.__file__, str(log_path), decision])
    return scenarios.hooks_config(command)


def _hook_log_events(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(json.loads(line)["text"]) for line in log_path.read_text().splitlines()]


def _pretooluse_events(log_path: Path) -> list[dict[str, Any]]:
    return [event for event in _hook_log_events(log_path) if event.get("hook_event_name") == "PreToolUse"]


@pytest.mark.parametrize(
    ("decision", "output_markers", "final_text"),
    [
        pytest.param("allow", ["PROBE_OK", "exited with code 0"], "PROBE_DONE", id="allow"),
        pytest.param(
            "deny", ["Command blocked by PreToolUse hook", codex_hook.DENIAL], "HOOK_DENIED_OBSERVED", id="deny"
        ),
    ],
)
async def test_pretooluse_decision_gates_the_tool(
    codex: CodexHarness,
    openai_responses: OpenAIResponses,
    tmp_path: Path,
    decision: str,
    output_markers: list[str],
    final_text: str,
) -> None:
    log_path = tmp_path / "hooks.jsonl"
    async with codex.start(openai_responses, config=_hooks_config(log_path, decision)) as run:
        turn = await run.start_turn("Use the shell probe and report the outcome.")

        async with await openai_responses.await_next_request() as exchange:
            stream = responses_sse.response_stream(
                [responses_sse.FunctionCall("call_test_1", "exec_command", {"cmd": PROBE})], model=MODEL
            )
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            (result,) = exchange.request.function_call_outputs
            assert result.call_id == "call_test_1"
            for marker in output_markers:
                assert marker in result.output
            stream = responses_sse.response_stream([responses_sse.Message(final_text)], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    frames.assert_success(captured, final_text)

    assert len(_pretooluse_events(log_path)) == 1


if __name__ == "__main__":
    pytest_bazel.main()
