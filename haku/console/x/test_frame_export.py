"""What comes out of exporting a recorded session, and what does not.

The export turns production traffic into a git object, so what matters is the traffic that must not
survive it and the structure that must.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
import pytest_bazel
from more_itertools import one

from haku.console.chat_models import SPA_ORIGIN, FrameDirection
from haku.console.x import frame_export
from haku.console.x.claude_code import frames
from haku.console.x.claude_code.projection import RecordedFrame, project_log
from haku.console.x.conversation_events import ToolCallCompleted, ToolCallStarted
from haku.console.x.frame_projection import projected
from haku.console.x.session_store import BridgeAuthentication, SpaSession
from haku.console.x.setup_output import SETUP_OUTPUT_KIND, setup_output_frame

# A bearer smuggled into the log the only realistic way one gets there: an operator ran a command
# that carried it. If this string reaches an exported line, the export is unusable.
SECRET = "sk-ant-oat01-DO-NOT-PUBLISH-THIS-VALUE"


def _assistant(*blocks: dict[str, Any], message_id: str) -> dict[str, Any]:
    """One `assistant` frame. Every frame needs its own *message_id*: `frame_uid` dedupes on it, so
    two frames sharing one are one frame to the recorder."""
    return {"type": "assistant", "message": {"id": message_id, "role": "assistant", "content": list(blocks)}}


def _tool_result(call_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}]},
        "tool_use_result": {"exit_code": 0, "stdout": text},
    }


# A turn that backgrounds a command and then dies mid-answer: thinking, a `tool_use` carrying a
# secret, its result, the harness narrating the step, and a delta of an answer that never completed.
SESSION_FRAMES: list[dict[str, Any]] = [
    _assistant({"type": "thinking", "thinking": "which shell"}, message_id="msg_1"),
    _assistant(
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "Bash",
            "input": {"command": f"curl -H 'Authorization: Bearer {SECRET}' https://api", "run_in_background": True},
        },
        message_id="msg_2",
    ),
    _tool_result("toolu_1", "Running in background with ID bash_1"),
    {
        "type": "system",
        "subtype": "task_started",
        "task_id": "task_1",
        "task_type": "local_bash",
        "description": "curl",
    },
    {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "started"}},
    },
]


@pytest.fixture
async def exported(chat_store, migrated_sessions, operator_id) -> frame_export.ExportedSession:
    """One session recorded through the write path, with a console-authored `setup_output` row in it."""
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id: UUID = view.session_id
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("cloning the repo")
    )
    await chat_store.enqueue_prompt(operator_id, session_id, "start the build", SPA_ORIGIN)
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    for payload in SESSION_FRAMES:
        recorded = await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, payload["type"], payload)
        await chat_store.apply_frame(
            session_id, started.turn_id, recorded.frame_seq, projected(frame_seq=recorded.frame_seq, payload=payload)
        )
    async with migrated_sessions() as db:
        return await frame_export.export_session(db, session_id)


def _reread(exported: frame_export.ExportedSession) -> list[RecordedFrame]:
    """The fixture as `test_diverse_session.py` reads one: a record's index is its `frame_seq`."""
    return [
        RecordedFrame(frame_seq=index, payload=json.loads(line)["frame"]) for index, line in enumerate(exported.lines())
    ]


def test_a_secret_in_a_tool_argument_does_not_reach_the_fixture(exported) -> None:
    """Asserted over the file's own bytes rather than over any one field: a command line elides
    because nothing said to keep it."""
    assert SECRET not in "\n".join(exported.lines())

    call = one(
        block
        for frame in _reread(exported)
        for block in frames.content_blocks(frame.payload)
        if block.get("type") == "tool_use"
    )
    # The tool's name and the flag are the shape a fixture is for; the command is the operator's.
    assert (call["name"], call["input"]["run_in_background"]) == ("Bash", True)
    assert call["input"]["command"].startswith("<elided:")


def test_the_console_authored_row_is_left_out(exported) -> None:
    """`setup_output` carries no protocol `type`, so the fold does not read it — and a fixture
    holding it would be a fixture of something the wire never sent."""
    assert [frame.payload["type"] for frame in _reread(exported)] == [
        "assistant",
        "assistant",
        "user",
        "system",
        "stream_event",
    ]


def test_redaction_leaves_the_projection_alone(exported) -> None:
    """Structure is what a fixture is for, so the redacted frames must fold to the same kinds in the
    same order — not to the same events, since the prose is gone by construction.
    """
    original = project_log(RecordedFrame(frame_seq=seq, payload=payload) for seq, payload in enumerate(SESSION_FRAMES))
    redacted = project_log(_reread(exported))

    assert [type(event) for event in redacted.events] == [type(event) for event in original.events]
    assert dict(redacted.unprojected) == dict(original.unprojected)


def test_a_call_and_its_answer_still_pair_after_pseudonymisation(exported) -> None:
    """One identifier, two frames: eliding it would leave a fixture unable to say which result
    answered which call."""
    events = project_log(_reread(exported)).events

    started = one(event for event in events if isinstance(event, ToolCallStarted))
    completed = one(event for event in events if isinstance(event, ToolCallCompleted))
    assert started.call_id == completed.call_id
    assert started.call_id != "toolu_1"


def test_the_offsets_are_relative_to_the_first_exported_frame(exported) -> None:
    """Wall-clock says when an operator was working; an offset says how far apart two frames were,
    which is what a background command's fixture needs."""
    offsets = [json.loads(line)["t"] for line in exported.lines()]

    assert offsets[0] == 0.0
    assert offsets == sorted(offsets)


def test_the_summary_counts_what_it_wrote(exported) -> None:
    assert str(exported.session_id) in exported.summary()
    assert "assistant×2" in exported.summary()


if __name__ == "__main__":
    pytest_bazel.main()
