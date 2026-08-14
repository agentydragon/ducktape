"""What identifies one frame of the CLI's conversation channel, for the frames that have it.

**Replay is safe because a frame carries identity, not because a cursor is exact.** A console can
die between recording a frame and acknowledging it, so a resumed connection replays whatever fell
in that gap however careful the cursor was. Making the cursor exact is therefore an optimisation;
what makes replay *correct* is being able to recognise a frame already seen.

The identities below are the agent's own — every one of them is a field the CLI already put on the
wire, not something this console assigns. That matters: an id we minted would identify our receipt
of a frame rather than the frame, and two receipts of one frame are exactly what has to collapse.

**`stream_event` has none, and must not be given one.** A delta is meaningless alone — the console
reconstructs an answer with `streamed += delta`, so replaying one double-appends — and it is also
the one class that never needs replaying, because it is superseded by the completed `assistant`
frame that follows. The recorder already drops deltas for an unrelated reason, so "buffer and
replay everything except deltas" is a rule the system was keeping anyway.

<protocol.md> describes the wire these fields come from.
"""

from __future__ import annotations

from typing import Any

# The kinds this console keeps that are not the CLI's protocol at all: a line the sandbox printed,
# and the console's own reconstruction of an answer still arriving. Neither crosses the bridge as a
# conversation frame, so neither is ever replayed, and neither has an agent-assigned id to use.
CONSOLE_AUTHORED_KINDS = frozenset({"setup_output", "partial"})


def frame_uid(kind: str, payload: dict[str, Any]) -> str | None:
    """The agent's own identity for this frame, or None when it has none to give.

    None is not a failure and not a fallback — it is the honest answer for a delta, for the two
    kinds this console authors itself, and for any frame kind the CLI adds that this release has
    not been taught. A caller dedupes on what it gets and lets the rest through, which is the safe
    direction: a duplicate that slips past is one repeated line in a rollout, while a collision
    invented to avoid that would drop a frame that never arrived twice.
    """
    match kind:
        case "assistant" | "user" if isinstance(message := payload.get("message"), dict):
            # The assistant's own message id, and for a `user` frame the id of the call it
            # answers — which is what pairs a result with the request in the rollout, and is why
            # `user` reads its identity out of the content rather than the envelope.
            if (message_id := message.get("id")) is not None:
                return f"{kind}:{message_id}"
            return _tool_result_uid(kind, message)
        case "result":
            # One `result` ends one turn, and the CLI's session id plus the turn's own uuid is
            # what distinguishes it from the result of the turn before.
            if (uuid := payload.get("uuid")) is not None:
                return f"result:{uuid}"
        case "command_lifecycle":
            # A pair, because one command reports several states and each is its own frame.
            if (command := payload.get("command_uuid")) is not None and (state := payload.get("state")) is not None:
                return f"command:{command}:{state}"
        case "system":
            if (task_id := payload.get("task_id")) is not None:
                return f"system:{payload.get('subtype')}:{task_id}"
    return None


def _tool_result_uid(kind: str, message: dict[str, Any]) -> str | None:
    """A `user` frame's identity: the `tool_use_id` of the result it carries.

    A turn's tool results arrive as `user` frames with no id of their own, so the call being
    answered is the only agent-assigned thing on them. A frame carrying several is identified by
    the first, which is enough: the CLI emits them in one block per call, so two frames sharing a
    first `tool_use_id` are the same frame.
    """
    if kind != "user" or not isinstance(content := message.get("content"), list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result" and (used := block.get("tool_use_id")):
            return f"tool_result:{used}"
    return None
