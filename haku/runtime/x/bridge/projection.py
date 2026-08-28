"""One harness observation's neutral yield, and the small helpers a projector shares.

A projector folds one harness's native stream into the operations of <neutral_operations.py>. What
it returns for each observation is a `Projected`; how it accumulates one is a `Yield`. Both are
harness-invariant, so Claude's projector (<claude_projection.py>) and Codex's
(<codex_projection.py>) share them rather than each minting its own — as do the frame-span and
delta-overlap helpers every projector needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from haku.runtime.x.bridge.neutral_operations import FrameRange, Operation


@dataclass(frozen=True, slots=True)
class Projected:
    """One observation's neutral yield: operations in order, and what could not be said.

    `unprojected` counts by frame class, in the projector's own vocabulary; deliberately ignored
    classes are not in it. The journal accumulates both into batches.
    """

    operations: tuple[Operation, ...]
    unprojected: Mapping[str, int]


@dataclass(slots=True)
class Yield:
    """One `observe`/`admit` call's accumulating output."""

    operations: list[Operation] = field(default_factory=list)
    unprojected: dict[str, int] = field(default_factory=dict)

    def miss(self, key: str) -> None:
        self.unprojected[key] = self.unprojected.get(key, 0) + 1

    def projected(self) -> Projected:
        return Projected(operations=tuple(self.operations), unprojected=self.unprojected)


def at(frame_seq: int) -> FrameRange:
    """A single-frame provenance span."""
    return FrameRange(first_frame_seq=frame_seq, last_frame_seq=frame_seq)


def undelivered(text: str, delivered: str) -> str:
    """The part of a completed block nobody has been shown yet.

    A block's deltas deliver its prose as it is written and the completed block repeats all of it,
    so emitting the block whole would say the answer twice. The overlap is subtracted rather than a
    prefix length assumed: the full watermark mid-stream, nothing where a block streamed no deltas,
    and no double print where the two texts disagree in a way a prefix test would miss.
    """
    overlap = min(len(text), len(delivered))
    while overlap and not delivered.endswith(text[:overlap]):
        overlap -= 1
    return text[overlap:]
