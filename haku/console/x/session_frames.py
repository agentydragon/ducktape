"""What goes in the `session_frames` log, and how to read one back out.

The kinds a row's `kind` column carries, the two frames the console authors itself, and the
readers that pick a value out of a payload. Everything here is about the wire's own shapes, so
it holds no session state and touches no table — `claude_chat.py`'s store and turn loop are
what put these rows in and take them out.
"""

from __future__ import annotations

from typing import Any

# TODO(frame-vocabulary): these are not one vocabulary, and there is deliberately no enum over
# them yet. Five of them are the CLI's own top-level `type`; `SETUP_OUTPUT_KIND` is the *bridge*
# envelope's `kind` literal, put in the same column by a different sink. An enum over the union
# would give a name to a concept the schema does not actually have — see `SessionFrame` and
# stage 2 of <../../plans/chat_runtime_projection.md>, which is where this becomes one thing.

# One token batch of an answer still being written. Hundreds per turn, and the completed
# `assistant` frame repeats all of it, which is why `read_frames` leaves them out of its default
# view.
DELTA_FRAME_KIND = "stream_event"

# The frame a prompt crosses the wire as. Only meaningful with a direction beside it: the CLI
# sends `user` frames too, carrying tool results.
PROMPT_FRAME_KIND = "user"

# The frame that ends a turn, and the one that completes an assistant message. Both are read back
# out of the log by `adopt_open_turn` to work out what a departed holder had got to.
RESULT_FRAME_KIND = "result"
ASSISTANT_FRAME_KIND = "assistant"

# The bridge's, not the CLI's — see the TODO above.
SETUP_OUTPUT_KIND = "setup_output"


def assistant_frame(text: str) -> dict[str, Any]:
    """The frame shape the agent will send, for the one the console stands in for meanwhile.

    Same shape as the wire's, so a reader needs no second case; the row's `partial` column is
    what says it was reconstructed rather than observed.
    """
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def setup_output_frame(text: str) -> dict[str, Any]:
    """One line the sandbox printed, as a rollout row.

    **Console-authored, like `partial`, and it says so with its discriminator.** The bridge's
    own frame is `SetupOutput(data: bytes)` — raw, unsplit, base64 on the wire — and what
    arrives here is one line the transport has already decoded (`errors="replace"`) and split
    for the room. So this is a rendering, not the wire, and putting it under `kind` rather than
    the CLI's `type` is what keeps it from reading as a protocol frame that never existed.

    It lives in the frame log rather than a table of its own because the question a reader asks
    is "what happened in this session, in order" — and for a session that died before the CLI
    produced anything, the answer is entirely here.
    """
    return {"kind": SETUP_OUTPUT_KIND, "text": text}


def frame_kind(payload: dict[str, Any]) -> str:
    kind = payload.get("type")
    if not isinstance(kind, str):
        raise ValueError(f"protocol frame has no type: {payload=}")
    return kind


def agent_message_id(frame: dict[str, Any]) -> str | None:
    """The agent's own id for an `assistant` frame's message, if it carried one."""
    message = frame.get("message")
    return str(agent_id) if isinstance(message, dict) and (agent_id := message.get("id")) else None


def content_blocks(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """The content blocks of an `assistant` frame, or none if it carries none.

    Tolerant rather than strict: this reads the wire, where a block type we have never seen is
    a new CLI feature and not a bug in us. The frame itself is already recorded verbatim, so
    anything skipped here is still in the rollout.
    """
    message = frame.get("message")
    if not isinstance(message, dict):
        return []
    return [block for block in message.get("content", []) if isinstance(block, dict)]


def text_delta(event: dict[str, Any]) -> str:
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""
