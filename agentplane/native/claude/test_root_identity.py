"""A fresh Claude root's session ID appears on the first input, not initialization.

With and without a preselected ID, the test checks initialization frames before any model request,
then verifies the first input/result carries a UUID and preserves the requested value when supplied.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest
import pytest_bazel

from agentplane.native.claude.testing import anthropic_sse
from agentplane.native.claude.testing.harness import ClaudeLauncher
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.claude.testing.model import MODEL


@pytest.mark.parametrize("preselect_id", [False, True])
@pytest.mark.parametrize("send_input", [False, True])
async def test_fresh_root_identity_timing(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages, preselect_id: bool, send_input: bool
) -> None:
    requested_id = str(uuid4()) if preselect_id else None
    async with claude.start(anthropic_messages, session_id=requested_id) as run:
        # Explicit negative observation window, not a sleep used to infer work
        # completion. Initialization must not manufacture a model prompt.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anthropic_messages.await_next_request(), timeout=0.25)
        initialized = run.native_frames()
        assert any(frame.get("type") == "control_response" for frame in initialized)
        before_input_ids = {frame["session_id"] for frame in initialized if frame.get("session_id")}
        (claude.logs / "identity-before-input.json").write_text(
            json.dumps({"requested_id": requested_id, "observed_ids": sorted(before_input_ids)}, indent=2) + "\n"
        )
        # This is about what the wire declares, not whether internal state already
        # has an allocated ID. The observation is bounded to this initialization.
        assert before_input_ids == set()
        if send_input:
            async with asyncio.timeout(30):
                submitted = await run.send("Reply ROOT_ID_CONFIRMED.")
                async with await anthropic_messages.await_next_request() as exchange:
                    await exchange.send(
                        *anthropic_sse.message_stream([anthropic_sse.Text("ROOT_ID_CONFIRMED")], model=MODEL).events
                    )
                result = await submitted.result()
                assert result.result == "ROOT_ID_CONFIRMED"
                assert result.session_id is not None
                UUID(result.session_id)
                if requested_id is not None:
                    assert result.session_id == requested_id
                session_frames = [frame for frame in run.native_frames() if frame.get("session_id")]
                assert {frame["session_id"] for frame in session_frames} == {result.session_id}
                (claude.logs / "identity-after-input.json").write_text(json.dumps(session_frames[0], indent=2) + "\n")
    # Native logs include graceful no-input shutdown as well; a post-shutdown
    # result must not retroactively be described as an initialization declaration.


if __name__ == "__main__":
    pytest_bazel.main()
