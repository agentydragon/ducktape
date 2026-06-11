from __future__ import annotations

from agent_core.handler import BaseHandler
from agent_core.loop_control import Abort, NoAction

"""Prompt Engineer loop controller(s).

These controllers implement minimal, application-level policies on top of the
Agent generic loop-control API without baking any app-specific behavior into
Agent itself.
"""


class ProposePromptNTimes(BaseHandler):
    """Require a tool call for the first N sampling steps, then abort.

    This is used by the Prompt Engineer to force exactly N calls to the
    propose_prompt MCP tool within a single outer `.run(...)` call. No transcript
    scraping is required; the MCP server maintains authoritative state.
    """

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError("n must be >= 1")
        self._n = n
        self._k = 0

    def on_before_sample(self):
        if self._k < self._n:
            self._k += 1
            return NoAction()
        return Abort()
