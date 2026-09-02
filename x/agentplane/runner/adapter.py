"""What a session asks of its provider adapter.

An abstract base rather than a union of the two adapters: the session module must not import the
adapters, which import it, so this is the seam that breaks the cycle.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import Any


class HarnessAdapter(abc.ABC):
    @abc.abstractmethod
    def command(self) -> list[str]: ...

    @abc.abstractmethod
    def environment(self) -> Mapping[str, str]: ...

    @abc.abstractmethod
    async def handshake(self) -> str:
        """Initialize the freshly started harness and return its native session id."""

    @abc.abstractmethod
    async def submit(self, input_id: str, text: str) -> None: ...

    @abc.abstractmethod
    async def interrupt(self) -> None: ...

    @abc.abstractmethod
    async def on_frame(self, frame: dict[str, Any]) -> None:
        """Translate one parsed stdout frame into session events, answering the harness if it asked."""
