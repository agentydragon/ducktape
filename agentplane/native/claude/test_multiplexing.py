"""Claude Code 2.1.252 has one root session per stream-json process.

The test probes a second input carrying a different `session_id` while the first root is active,
then checks that two separately launched roots can make concurrent model requests with distinct
model, workdir, and config-directory settings. It does not test different credentials, sharing one
config directory between processes, or multi-root crash recovery.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.native.claude import wire
from agentplane.native.claude.testing import anthropic_sse
from agentplane.native.claude.testing.harness import ClaudeLauncher
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.claude.testing.model import MODEL

SECONDARY_MODEL = "agentplane-multiplex/claude-haiku-4-5-20251001"
FIRST_PROMPT = "Reply with exactly: CLAUDE_MULTIPLEX_FIRST"
SECOND_ROOT_PROMPT = "Reply with exactly: CLAUDE_MULTIPLEX_SECOND_ROOT"
SECOND_PROCESS_PROMPT = "Run pwd, then reply with exactly: CLAUDE_MULTIPLEX_SECOND_PROCESS"


class SessionSelectedUserInput(wire.UserInput):
    """Probe whether the pinned CLI treats a per-input session_id as a routing selector."""

    session_id: str


async def test_one_root_per_process_and_separate_processes_are_independent(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    first_session_id = str(uuid4())
    attempted_session_id = str(uuid4())
    second_process_session_id = str(uuid4())

    second_workspace = claude.workspace.parent / "secondary-workspace"
    second_workspace.mkdir()
    second_config = claude.config.parent / ".claude-secondary"
    second_config.mkdir()
    second_logs = claude.logs / "secondary"
    second_logs.mkdir()
    second_claude = replace(claude, workspace=second_workspace, config=second_config, logs=second_logs)

    async with (
        claude.start(anthropic_messages, session_id=first_session_id, replay_user_messages=True) as first_run,
        second_claude.start(
            anthropic_messages, session_id=second_process_session_id, replay_user_messages=True, model=SECONDARY_MODEL
        ) as second_run,
    ):
        first = await first_run.send(FIRST_PROMPT)
        async with await anthropic_messages.await_next_request() as first_exchange:
            assert first_exchange.request.model == MODEL
            assert first_exchange.request.texts("user")[-1] == FIRST_PROMPT
            first_stream = anthropic_sse.message_stream(
                [anthropic_sse.Text("CLAUDE_MULTIPLEX_FIRST_DONE")], model=MODEL
            )
            prefix = first_stream.through("content_block_start")
            await first_exchange.send(*prefix.events)
            await first.active()

            # `session_id` appears on native output, but is not a field in Claude's typed user
            # input. Probe it directly instead of treating the driver's current input model as
            # proof that the CLI cannot route a second root.
            attempted_root = await first_run.send_frame(
                SessionSelectedUserInput(
                    message=wire.UserMessage(role="user", content=SECOND_ROOT_PROMPT),
                    uuid=str(uuid4()),
                    session_id=attempted_session_id,
                )
            )
            assert (await attempted_root.lifecycle(wire.CommandState.QUEUED)).session_id == first_session_id

            # The second frame joins the active process's one input queue; it does not create a
            # concurrent model request for the attempted root.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anthropic_messages.await_next_request(), timeout=0.25)

            second = await second_run.send(SECOND_PROCESS_PROMPT)
            async with await anthropic_messages.await_next_request() as second_exchange:
                assert second_exchange.request.model == SECONDARY_MODEL
                assert second_exchange.request.texts("user")[-1] == SECOND_PROCESS_PROMPT
                await second_exchange.send(
                    *anthropic_sse.message_stream(
                        [anthropic_sse.ToolUse("toolu_secondary_pwd", "Bash", {"command": "pwd"})],
                        model=SECONDARY_MODEL,
                    ).events
                )

            async with await anthropic_messages.await_next_request() as second_followup:
                assert second_followup.request.model == SECONDARY_MODEL
                assert SECOND_PROCESS_PROMPT in second_followup.request.texts("user")
                assert str(second_workspace) in second_followup.request.tool_results[0].text

                # Interrupting the first process cancels its queued input, while the independent
                # second process still owns a live request that can complete normally.
                assert (await first_run.interrupt(cancel_queued=True)).response.subtype == "success"
                await first_exchange.wait_client_closed()
                assert (await first.result()).is_error
                assert (await attempted_root.lifecycle(wire.CommandState.CANCELLED)).session_id == first_session_id
                assert second_run.running

                await second_followup.send(
                    *anthropic_sse.message_stream(
                        [anthropic_sse.Text("CLAUDE_MULTIPLEX_SECOND_PROCESS")], model=SECONDARY_MODEL
                    ).events
                )
                await second_followup.close()
                assert (await second.result()).result == "CLAUDE_MULTIPLEX_SECOND_PROCESS"

    first_frame_ids = {frame["session_id"] for frame in first_run.native_frames() if frame.get("session_id")}
    second_frame_ids = {frame["session_id"] for frame in second_run.native_frames() if frame.get("session_id")}
    assert first_frame_ids == {first_session_id}
    assert second_frame_ids == {second_process_session_id}

    (first_transcript,) = claude.config.glob(f"projects/*/{first_session_id}.jsonl")
    (second_transcript,) = second_config.glob(f"projects/*/{second_process_session_id}.jsonl")
    assert FIRST_PROMPT in first_transcript.read_text()
    second_history = second_transcript.read_text()
    assert SECOND_PROCESS_PROMPT in second_history
    assert FIRST_PROMPT not in second_history
    assert SECOND_ROOT_PROMPT not in second_history
    assert not list(claude.config.glob(f"projects/*/{attempted_session_id}.jsonl"))


if __name__ == "__main__":
    pytest_bazel.main()
