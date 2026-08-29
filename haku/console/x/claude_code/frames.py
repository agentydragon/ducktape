"""The CLI's own frame vocabulary, and the readers that pick a value out of one.

The `type` values Claude Code puts at the top of a frame, and the shapes underneath them. The
console stores the complete native object in `session_frames.payload`; `session_frames.kind` remains
only the outer session-frame class. Native kinds are derived when an inspection or projection needs
them, so a later CLI value remains preserved even before this module learns its meaning.

Everything here is about the wire's shapes, so it holds no session state and touches no table.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# One token batch of an answer still being written. Hundreds per turn, and the completed
# `assistant` frame repeats all of it; Claude's projector decides which copy becomes a neutral
# segment while the generic raw-frame reader preserves both.
DELTA_FRAME_KIND = "stream_event"

# The frame a prompt crosses the wire as. Only meaningful with a direction beside it: the CLI
# sends `user` frames too, carrying tool results.
PROMPT_FRAME_KIND = "user"

# The frame that ends a turn, and the one that completes an assistant message. Both are the
# projection's to read: a turn's ending reaches the console as the frame that closes it, live or
# replayed, rather than being reconstructed out of the log.
RESULT_FRAME_KIND = "result"
ASSISTANT_FRAME_KIND = "assistant"


class ResultFrame(BaseModel):
    """The typed fields Console reads from Claude Code's turn-ending frame.

    Extra fields remain accepted because the raw ``HarnessFrame`` is the durable wire record and a
    newer CLI adding telemetry must not make an otherwise readable result fail closed.
    """

    model_config = ConfigDict(extra="allow")

    type: Literal["result"]
    subtype: str
    result: Any = None
    stop_reason: str | None = None


def frame_kind(payload: dict[str, Any]) -> str:
    kind = payload.get("type")
    if isinstance(kind, str):
        return kind
    method = payload.get("method")
    return method if isinstance(method, str) else "<undiscriminated>"


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
