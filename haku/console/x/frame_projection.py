"""The fold as the write path configures it: one recorded frame in, neutral events out.

**The one function the writer and any re-projection share.** `session_runtime`'s turn loop calls it
per frame and `session_store.apply_frame` stores what it returns, so anything re-projecting a
session's frames to compare against those rows has to fold the same way — `reprojection` threads the
same state across a turn's frames for exactly that reason.

**The state is threaded across a turn's frames, and has to be.** Seeding empty per frame looks
appealing — the cursor would then rest on a state nothing carries — and it is wrong under the item
vocabulary in two ways that are not cosmetic. A `stream_event` frame carries a few characters and no
`message.id`, so an empty seed makes each delta open, speak and close an item of its own: hundreds
per turn, which is the shape the vocabulary exists to rule out. And a completed text block repeats
the prose its deltas already delivered, which only the watermark on the open item can subtract; with
no state there is no watermark, and the answer is stored twice.

What the cursor rests on instead is the store: an item's identity is its `item_id` and the turn's
open item is a row, so a fold resuming from a cursor mid-message writes onto what its predecessor
left open rather than needing to have been told which row that is
(`conversation_log.LogWriter._resume`). What it does have to be told is how much of that item has
already been said, which `session_runtime._inherited` seeds from the row.

`Projection.unprojected` is dropped here rather than logged: per frame in the hot path it would be a
log line for every heartbeat. It is read on the two paths that re-fold stored frames instead —
`session_store.read_transcript`'s `unreadable`, and the frame inspector's per-frame count
(`session_views.frame_page`).
"""

from __future__ import annotations

from typing import Any

from haku.console.x.claude_code import projection
from haku.console.x.conversation_events import ConversationEvent, ProjectionState


def projected(
    state: ProjectionState, *, frame_seq: int, payload: dict[str, Any]
) -> tuple[ProjectionState, tuple[ConversationEvent, ...]]:
    """What one frame means, in the vocabulary every surface and every backend shares.

    `DeltaSource.STREAM_EVENTS`, so an answer becomes visible as it is written rather than when its
    block completes. The completed block then adds only what the deltas did not deliver, which is
    the whole of it wherever a backend streams nothing.
    """
    folded, said = projection.project(
        state,
        [projection.RecordedFrame(frame_seq=frame_seq, payload=payload)],
        delta_source=projection.DeltaSource.STREAM_EVENTS,
    )
    return folded, said.events
