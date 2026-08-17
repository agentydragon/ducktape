"""The fold as the write path configures it: one recorded frame in, neutral events out.

**The one function the writer and any re-projection share.** `session_runtime`'s turn loop calls it
per frame and `session_store.apply_frame` stores what it returns, so anything re-projecting a
session's frames to compare against those rows has to fold the same way: `project_log` over a whole
session merges the frames sharing one `message.id` and cuts its deltas from completed blocks, which
this does not, and the two event sequences are both correct and different.
"""

from __future__ import annotations

from typing import Any

from haku.console.x.claude_code import projection
from haku.console.x.conversation_events import ConversationEvent


def projected(*, frame_seq: int, payload: dict[str, Any]) -> tuple[ConversationEvent, ...]:
    """What one frame means, in the vocabulary every surface and every backend shares.

    **One frame at a time, seeded empty and declared over** — `project_log` over the single frame
    handed in, so a message always ends at its own frame here. Two consequences an aligner can rely
    on: every event's frame range is `(frame_seq, frame_seq)`, and a `result` frame produces
    exactly one `TurnCompleted` and nothing else.

    **Threading one state across the turn is a two-line change and is deliberately not made.** It
    breaks two things in the loop rather than in the fold: a message would complete on the frame
    that *closed* it, so `source_last_frame_seq` would name that frame instead of the last one that
    built the message
    (`test_projected_assistant_message_points_to_the_frames_that_built_it` catches it); and under
    `STREAM_EVENTS` a `TextDelta` is keyed by the delta's own frame rather than the message's
    (`_stream_delta` has no id to group by), so two adjacent text messages would share one
    accumulator and the second's deltas would be discarded when the first resets. It also merges
    the frames sharing one `message.id` into a single row and gives the room one reply per message
    instead of per frame — both change what is stored and what is sent.

    Seeding empty per frame is also what the cursor rests on: the fold carries nothing across a
    frame boundary, so the state at any cursor position is the empty one and resuming needs no
    state beside the position.

    `Projection.unprojected` is dropped here rather than logged: per frame in the hot path it would
    be a log line for every heartbeat. It is read on the two paths that re-fold stored frames
    instead — `session_store.read_transcript`'s `unreadable`, and the frame inspector's per-frame
    count (`session_views.frame_page`).
    """
    return projection.project_log(
        [projection.RecordedFrame(frame_seq=frame_seq, payload=payload)],
        delta_source=projection.DeltaSource.STREAM_EVENTS,
    ).events
