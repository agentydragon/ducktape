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

    **Console-authored, and it says so with its discriminator.** The bridge's own frame is
    `SetupOutput(data: bytes)` — raw, unsplit, base64 on the wire — and what arrives here is one
    line the transport has already decoded (`errors="replace"`) and split for the room. So this is
    a rendering, not the wire, and putting it under `kind` rather than the CLI's `type` is what
    keeps it from reading as a protocol frame that never existed.

    It lives in the frame log because it **is** runner→console traffic: a `SetupOutput` envelope
    crossed the wire and only the splitting is ours. A fact the console is the sole witness to — a
    lease changing hands — is a `session_events` row on the `authored` arm instead
    (<../../plans/chat_runtime_projection.md> § stage 4).
    """
    return {"kind": SETUP_OUTPUT_KIND, "text": text}
