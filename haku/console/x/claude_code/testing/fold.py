"""A recorded capture, folded the way the live turn loop folds one.

Production reads a stored session out of `conversation_event` and re-folds no frames
(<../../transcript_entries.py>), so a capture is no longer evidence about any reader. What it is
still evidence about is the **adapter**: `_run_turn` drives `RuntimeTurnHandler.apply` one frame at
a time and acts on the neutral effects it returns, and `ClaudeTurnHandler.apply` is `project` over
that one frame with the reducer state kept between calls. This is that loop and nothing else.

**Nothing here declares the stream over**, because nothing in the console does. A turn ends on
`end_turn`, an item the frames left open simply never completes, and a transcript does not print
one — so a fold that closed the last message would assert a behaviour the console does not have.

**`unprojected` is accumulated here because `FrameEffects` does not carry it.** The count is the
adapter's own accounting of frame classes it has no case for, and these captures are where it is
asserted.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from types import MappingProxyType

from haku.console.x.claude_code.projection import DeltaSource, ProjectionState, RecordedFrame
from haku.console.x.conversation_events import ConversationEvent, Projection


def in_batches(
    batches: Iterable[Sequence[RecordedFrame]], *, delta_source: DeltaSource = DeltaSource.COMPLETED_BLOCKS
) -> Projection:
    """Successive batches through one reducer state, as the single projection they add up to.

    How the frames are cut into batches is what several tests vary: the reducer's contract is that
    the answer does not depend on it.
    """
    state = ProjectionState()
    events: list[ConversationEvent] = []
    unprojected: Counter[str] = Counter()
    for batch in batches:
        state, projected = state.advance(batch, delta_source=delta_source)
        events.extend(projected.events)
        unprojected.update(projected.unprojected)
    return Projection(events=tuple(events), unprojected=MappingProxyType(dict(unprojected)))


def whole_capture(
    frames: Iterable[RecordedFrame], *, delta_source: DeltaSource = DeltaSource.COMPLETED_BLOCKS
) -> Projection:
    """A capture one frame at a time, which is the batching the live loop uses."""
    return in_batches(([frame] for frame in frames), delta_source=delta_source)
