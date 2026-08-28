"""The one row the console authors into the `session_frames` log out of a bridge frame."""

from __future__ import annotations

from typing import Any

from haku.console.session.session_frames import BridgeFrameKind

SETUP_OUTPUT_KIND = BridgeFrameKind.SETUP_OUTPUT


def setup_output_frame(text: str) -> dict[str, Any]:
    """One line the sandbox printed, as a rollout row.

    **Console-authored, and it says so with its discriminator.** The bridge's own frame is
    `SetupOutput(data: bytes)` — raw, unsplit, base64 on the wire — and what arrives here is one
    line the transport has already decoded (`errors="replace"`) and split for the room. So this is
    a rendering rather than the wire, and it goes under `kind` rather than the CLI's `type` so it
    cannot read as a protocol frame that never existed.

    It lives in the frame log because it **is** runner→console traffic: a `SetupOutput` envelope
    crossed the wire and only the splitting is ours. A fact the console is the sole witness to — a
    lease changing hands — is a `conversation_event` row on the `authored` arm instead
    (<../conversation/conversation_event.py>).
    """
    return {"kind": SETUP_OUTPUT_KIND, "text": text}
