"""A lossless transport seam for typed native harness operations.

The transport does not interpret a harness's conversation.  Its owner records every raw
frame before making it available to a request matcher.  ``Session`` is that owner in the
runner; ``AsyncNativeProcess`` provides the same non-destructive contract to native
behavior tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

Frame = dict[str, Any]
FrameMatcher = Callable[[Frame], bool]


@dataclass(frozen=True)
class NativeReceipt:
    """One inbound raw frame and its source-local order.

    A runner session's sequence is its durable ``Native`` event sequence.  Native behavior
    tests use their append-only stdout trace's local ordinal instead; facades need only the
    receipt frame, while runner projections retain the durable sequence as causal evidence.
    """

    frame: Frame
    sequence: int


class NativeTransport(Protocol):
    """Write native frames and wait for an already-recorded matching receipt."""

    async def send(self, frame: BaseModel) -> None: ...

    async def request(self, frame: BaseModel, *, matches: FrameMatcher, timeout_s: float = 60) -> NativeReceipt: ...
