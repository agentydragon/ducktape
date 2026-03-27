"""Handler for enforcing maximum turn limits on agent execution."""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.handler import BaseHandler
from agent_core.loop_control import LoopDecision, NoAction


class MaxTurnsExceededError(Exception):
    """Raised when agent exceeds the maximum allowed turns."""


@dataclass
class MaxTurnsHandler(BaseHandler):
    """Handler that enforces a maximum number of sampling turns.

    Tracks sampling attempts and raises MaxTurnsExceededError when limit is reached.

    Args:
        max_turns: Maximum number of turns (1-999). Should be validated by caller via Pydantic Field(ge=1, lt=1000).
    """

    max_turns: int
    _turn_count: int = 0

    def on_before_sample(self) -> LoopDecision:
        """Track sampling attempts and enforce turn limit."""
        self._turn_count += 1
        if self._turn_count > self.max_turns:
            raise MaxTurnsExceededError(
                f"Agent exceeded maximum allowed turns ({self.max_turns}). "
                f"This likely indicates the agent is stuck in a loop or not following instructions."
            )
        return NoAction()
