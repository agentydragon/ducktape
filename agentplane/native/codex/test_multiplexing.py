"""Two independently addressed root threads over one pinned app-server process.

The scenario holds two model requests open, checks thread-attributed requests/events, interrupts one
turn while the other completes, then resumes both persisted IDs on a fresh process sharing
`CODEX_HOME`. It tests selected per-thread model, effort, instructions and `cwd`; both threads share
the process environment, provider and credentials. Separate auth identities and active multi-thread
crash recovery are not tested.

Pinned source references at Codex `rust-v0.157.0`, commit
[`00c972ed`](https://github.com/openai/codex/tree/00c972ed5d6ff6499317fd41b7f23605b8e6850d):
[`ThreadStartParams`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L62-L188)
includes thread-level `cwd`, model and developer instructions. [`TurnStartParams` and
`TurnInterruptParams`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L167-L335)
carry the addressed thread ID. The
[`ThreadStateManager`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/thread_state.rs#L343-L346)
tracks IDs per connection, and
[`shutdown_threads`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/request_processors/thread_processor.rs#L1281-L1291)
shuts down its managed threads together. That joint-shutdown behavior is source-only; the test does
not establish active multi-thread crash recovery.
"""

from __future__ import annotations

import pytest_bazel

from agentplane.native.codex import wire
from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses

SECONDARY_MODEL = "agentplane-multiplex-test-model"
SECONDARY_INSTRUCTIONS = "THREAD_LOCAL_INSTRUCTIONS: preserve the secondary workspace."
FIRST_PROMPT = "Reply with exactly: CODEX_MULTIPLEX_FIRST"
SECOND_PROMPT = "Run pwd, then reply with exactly: CODEX_MULTIPLEX_SECOND"
FIRST_RESUME_PROMPT = "Reply with exactly: CODEX_MULTIPLEX_FIRST_RESUMED"
SECOND_RESUME_PROMPT = "Reply with exactly: CODEX_MULTIPLEX_SECOND_RESUMED"


async def test_independent_threads_share_a_process_and_resume_by_thread_id(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    second_workspace = codex.workspace / "secondary-workspace"
    second_workspace.mkdir()

    async with codex.start(openai_responses, persist=True) as run:
        first = run.thread()
        second = await run.start_thread(
            cwd=str(second_workspace),
            model=SECONDARY_MODEL,
            effort="high",
            persist=True,
            instructions=SECONDARY_INSTRUCTIONS,
        )
        assert first.thread_id != second.thread_id

        first_turn = await first.start_turn(FIRST_PROMPT)
        assert (await first_turn.started()).params.thread_id == first.thread_id
        async with await openai_responses.await_next_request() as first_exchange:
            first_request = first_exchange.request
            assert first_request.model == MODEL
            assert first_request.client_metadata.thread_id == first.thread_id
            assert first_request.prompt_cache_key == first.thread_id
            assert first_request.messages("user")[-1].text == FIRST_PROMPT

            second_turn = await second.start_turn(SECOND_PROMPT)
            assert (await second_turn.started()).params.thread_id == second.thread_id
            async with await openai_responses.await_next_request() as second_exchange:
                second_request = second_exchange.request
                assert second_request.model == SECONDARY_MODEL
                assert second_request.reasoning_config is not None
                assert second_request.reasoning_config["effort"] == "high"
                assert second_request.client_metadata.thread_id == second.thread_id
                assert second_request.prompt_cache_key == second.thread_id
                assert [message.text for message in second_request.messages("developer")] == [SECONDARY_INSTRUCTIONS]
                assert second_request.messages("user")[-1].text == SECOND_PROMPT
                await second_exchange.send(
                    *responses_sse.response_stream(
                        [responses_sse.FunctionCall("call_secondary_pwd", "exec_command", {"cmd": "pwd"})],
                        model=SECONDARY_MODEL,
                    ).events
                )

            async with await openai_responses.await_next_request() as second_followup:
                followup_request = second_followup.request
                assert followup_request.client_metadata.thread_id == second.thread_id
                assert str(second_workspace) in followup_request.function_call_outputs[0].output

                assert (await first.interrupt(first_turn)).error is None
                first_completion = await first_turn.completed()
                assert first_completion.params.thread_id == first.thread_id
                assert first_completion.params.turn.status is wire.TurnStatus.INTERRUPTED
                await first_exchange.wait_client_closed()

                await second_followup.send(
                    *responses_sse.response_stream(
                        [responses_sse.Message("CODEX_MULTIPLEX_SECOND")], model=SECONDARY_MODEL
                    ).events
                )
            second_completion = await second_turn.completed()
            assert second_completion.params.thread_id == second.thread_id
            assert second_completion.params.turn.status is wire.TurnStatus.COMPLETED
            assert run.running

        client_frames = run.client_frames()
        start_frames = [frame for frame in client_frames if frame.get("method") == "thread/start"]
        assert len(start_frames) == 2
        assert len({frame["id"] for frame in start_frames}) == 2
        assert any(
            frame["params"]["cwd"] == str(second_workspace)
            and frame["params"]["model"] == SECONDARY_MODEL
            and frame["params"]["developerInstructions"] == SECONDARY_INSTRUCTIONS
            for frame in start_frames
        )

        turn_start_frames = [frame for frame in client_frames if frame.get("method") == "turn/start"]
        by_prompt = {frame["params"]["input"][0]["text"]: frame for frame in turn_start_frames}
        assert by_prompt[FIRST_PROMPT]["params"]["threadId"] == first.thread_id
        assert by_prompt[SECOND_PROMPT]["params"]["threadId"] == second.thread_id
        interrupt_frames = [frame for frame in client_frames if frame.get("method") == "turn/interrupt"]
        assert len(interrupt_frames) == 1
        assert interrupt_frames[0]["params"] == {"threadId": first.thread_id, "turnId": first_turn.id}

        turn_events = [
            frame for frame in run.native_frames() if frame.get("method") in {"turn/started", "turn/completed"}
        ]
        for method in ("turn/started", "turn/completed"):
            assert any(
                frame.get("method") == method
                and frame["params"]["threadId"] == first.thread_id
                and frame["params"]["turn"]["id"] == first_turn.id
                for frame in turn_events
            )
            assert any(
                frame.get("method") == method
                and frame["params"]["threadId"] == second.thread_id
                and frame["params"]["turn"]["id"] == second_turn.id
                for frame in turn_events
            )
        first_thread_id = first.thread_id
        second_thread_id = second.thread_id

    async with codex.start(openai_responses, resume_thread_id=first_thread_id) as resumed:
        second_resumed = await resumed.resume_thread(second_thread_id)
        assert resumed.thread_id == first_thread_id
        assert second_resumed.thread_id == second_thread_id
        resume_frames = [frame for frame in resumed.client_frames() if frame.get("method") == "thread/resume"]
        assert {frame["params"]["threadId"] for frame in resume_frames} == {first_thread_id, second_thread_id}

        first_resume_turn = await resumed.start_turn(FIRST_RESUME_PROMPT)
        async with await openai_responses.await_next_request() as first_resume_exchange:
            first_resume_request = first_resume_exchange.request
            assert first_resume_request.client_metadata.thread_id == first_thread_id
            assert first_resume_request.messages("user")[-1].text == FIRST_RESUME_PROMPT
            await first_resume_exchange.send(
                *responses_sse.response_stream(
                    [responses_sse.Message("CODEX_MULTIPLEX_FIRST_RESUMED")], model=first_resume_request.model
                ).events
            )
        assert (await first_resume_turn.completed()).params.thread_id == first_thread_id

        second_resume_turn = await second_resumed.start_turn(SECOND_RESUME_PROMPT)
        async with await openai_responses.await_next_request() as second_resume_exchange:
            second_resume_request = second_resume_exchange.request
            assert second_resume_request.model == SECONDARY_MODEL
            assert second_resume_request.client_metadata.thread_id == second_thread_id
            assert second_resume_request.messages("developer")[0].text == SECONDARY_INSTRUCTIONS
            assert second_resume_request.messages("user")[-1].text == SECOND_RESUME_PROMPT
            await second_resume_exchange.send(
                *responses_sse.response_stream(
                    [responses_sse.Message("CODEX_MULTIPLEX_SECOND_RESUMED")], model=SECONDARY_MODEL
                ).events
            )
        assert (await second_resume_turn.completed()).params.thread_id == second_thread_id


if __name__ == "__main__":
    pytest_bazel.main()
