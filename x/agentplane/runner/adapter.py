"""What a session asks of its harness adapter.

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
    async def submit(self, command_id: str, text: str) -> None: ...

    @abc.abstractmethod
    async def interrupt(self, turn_id: str) -> None: ...

    @abc.abstractmethod
    async def change_model(self, command_id: str, model: str) -> None:
        """Apply one model command, reporting its causal effect through the owning Session."""

    @abc.abstractmethod
    async def on_frame(self, frame: dict[str, Any], source_sequence: int) -> None:
        """Translate one parsed stdout frame into session events, answering the harness if it asked.

        ``source_sequence`` is the durable Native Event which carried ``frame``. An adapter may
        wait for a small native cohort before emitting one observation, so it must retain the
        source rather than relying on the Session's ambient translating frame.
        """
