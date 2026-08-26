"""A recorded capture, folded the way the live turn loop folds one.

The Codex half of <../../claude_code/testing/fold.py>, and the same reasoning: production reads a
stored session out of `conversation_event`, so what a capture is evidence about is the adapter, and
`CodexTurnHandler.apply` is `project` over one frame with the reducer state kept between calls.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from types import MappingProxyType

from haku.console.x.codex_app_server.projection import ProjectionState, RecordedFrame
from haku.console.x.conversation_events import ConversationEvent, Projection


def in_batches(batches: Iterable[Sequence[RecordedFrame]]) -> Projection:
    """Successive batches through one reducer state, as the single projection they add up to."""
    state = ProjectionState()
    events: list[ConversationEvent] = []
    unprojected: Counter[str] = Counter()
    for batch in batches:
        state, projected = state.advance(batch)
        events.extend(projected.events)
        unprojected.update(projected.unprojected)
    return Projection(events=tuple(events), unprojected=MappingProxyType(dict(unprojected)))


def whole_capture(frames: Iterable[RecordedFrame]) -> Projection:
    """A capture one frame at a time, which is the batching the live loop uses."""
    return in_batches([frame] for frame in frames)
