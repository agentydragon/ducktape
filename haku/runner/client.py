"""Provider-neutral client types at the Console/runner boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from haku.runner.protocol import HarnessFrame


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    """Where a sink put one envelope, and whether the caller should act on it."""

    fresh: bool
    frame_seq: int


class FrameSink(Protocol):
    """Durable recording for typed harness envelopes in both directions."""

    async def sent(self, frame: HarnessFrame) -> int: ...

    async def received(self, frame: HarnessFrame) -> RecordedFrame: ...


@dataclass(frozen=True, slots=True)
class SentPrompt:
    """One provider prompt and the durable frame position assigned to it."""

    frame_seq: int


@dataclass(frozen=True, slots=True)
class ReceivedFrame:
    """One typed runner envelope and the Console log position assigned to it."""

    envelope: HarnessFrame
    frame_seq: int
