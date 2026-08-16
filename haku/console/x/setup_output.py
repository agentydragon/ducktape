"""The one row the console authors into the `session_frames` log out of a bridge frame."""

from __future__ import annotations

from typing import Any

# TODO(frame-vocabulary): `session_frames.kind` holds two discriminator vocabularies at once, and
# there is deliberately no enum over the union. This literal is the *bridge* envelope's; the CLI's
# own top-level `type` (<claude_code/frames.py>) is put in the same column by a different sink.
# Naming the union would name a concept the schema does not have — see `SessionFrame` and stage 2
# of <../../plans/chat_runtime_projection.md>, where the CLI's type gets its own column and this
# becomes one thing.
SETUP_OUTPUT_KIND = "setup_output"


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
