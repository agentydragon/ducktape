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

import pytest_bazel

from x.agentplane.capture import codex_hook
from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import MODEL, CodexHarness
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.codex import scenarios, wire

PROBE = "printf 'PROBE_OK\\n'"


def _hooks_config(log_path: Path, decision: str) -> dict[str, Any]:
    command = shlex.join([sys.executable, codex_hook.__file__, str(log_path), decision])
    return scenarios.hooks_config(command)


def _hook_log_events(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(json.loads(line)["text"]) for line in log_path.read_text().splitlines()]


async def test_pretooluse_allow_lets_the_tool_run(
    codex: CodexHarness, openai_responses: OpenAIResponses, tmp_path: Path
) -> None:
    log_path = tmp_path / "hooks.jsonl"
    async with codex.start(openai_responses, config=_hooks_config(log_path, "allow")) as run:
        turn = await run.start_turn("Use the shell probe and report PROBE_DONE.")

        async with await openai_responses.await_next_request() as exchange:
            stream = sse.response_stream([sse.FunctionCall("call_test_1", "exec_command", {"cmd": PROBE})], model=MODEL)
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            (result,) = exchange.request.function_call_outputs
            assert result.call_id == "call_test_1"
            assert "PROBE_OK" in result.output
            assert "exited with code 0" in result.output
            stream = sse.response_stream([sse.Message("PROBE_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    frames.assert_success(captured, "PROBE_DONE")

    events = _hook_log_events(log_path)
    pretooluse = [event for event in events if event.get("hook_event_name") == "PreToolUse"]
    assert len(pretooluse) == 1


async def test_pretooluse_deny_blocks_the_tool_and_the_model_sees_why(
    codex: CodexHarness, openai_responses: OpenAIResponses, tmp_path: Path
) -> None:
    log_path = tmp_path / "hooks.jsonl"
    async with codex.start(openai_responses, config=_hooks_config(log_path, "deny")) as run:
        turn = await run.start_turn("Use the shell probe and report the outcome.")

        async with await openai_responses.await_next_request() as exchange:
            stream = sse.response_stream([sse.FunctionCall("call_test_1", "exec_command", {"cmd": PROBE})], model=MODEL)
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            (result,) = exchange.request.function_call_outputs
            assert result.call_id == "call_test_1"
            assert "Command blocked by PreToolUse hook" in result.output
            assert codex_hook.DENIAL in result.output
            stream = sse.response_stream([sse.Message("HOOK_DENIED_OBSERVED")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    frames.assert_success(captured, "HOOK_DENIED_OBSERVED")

    events = _hook_log_events(log_path)
    pretooluse = [event for event in events if event.get("hook_event_name") == "PreToolUse"]
    assert len(pretooluse) == 1


if __name__ == "__main__":
    pytest_bazel.main()
