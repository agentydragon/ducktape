"""Handlers used by agent_core tests and test fixtures."""

from __future__ import annotations

from agent_core.events import AssistantText
from agent_core.handler import BaseHandler
from agent_core.loop_control import Abort, LoopDecision, NoAction


class FinishOnTextMessageHandler(BaseHandler):
    """Abort the agent loop when assistant sends a text message."""

    def __init__(self) -> None:
        self._text_detected = False

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._text_detected = True

    def on_before_sample(self) -> LoopDecision:
        if self._text_detected:
            self._text_detected = False
            return Abort()
        return NoAction()
