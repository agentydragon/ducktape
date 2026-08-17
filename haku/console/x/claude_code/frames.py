"""The CLI's own frame vocabulary, and the readers that pick a value out of one.

The `type` values Claude Code puts at the top of a frame, and the shapes underneath them. The
console stores these in `session_frames.kind` alongside the bridge's own envelope discriminator
(<../setup_output.py>), which is the two-vocabulary collision that giving the CLI's type its own
column resolves (<../../plans/conversation_layers.md> § 13).

Everything here is about the wire's shapes, so it holds no session state and touches no table.
"""

from __future__ import annotations

from typing import Any

# One token batch of an answer still being written. Hundreds per turn, and the completed
# `assistant` frame repeats all of it, which is why `read_frames` leaves them out of its default
# view.
DELTA_FRAME_KIND = "stream_event"

# The frame a prompt crosses the wire as. Only meaningful with a direction beside it: the CLI
# sends `user` frames too, carrying tool results.
PROMPT_FRAME_KIND = "user"

# The frame that ends a turn, and the one that completes an assistant message. Both are the
# projection's to read: a turn's ending reaches the console as the frame that closes it, live or
# replayed, rather than being reconstructed out of the log.
RESULT_FRAME_KIND = "result"
ASSISTANT_FRAME_KIND = "assistant"


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
