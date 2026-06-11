"""Token budget handler: warns at 50%, 90%; forces submission at 100%."""

from __future__ import annotations

import logging
from enum import Enum, auto

from agent_core.events import AssistantText, ReasoningItem, Response, ToolCall
from agent_core.handler import BaseHandler
from agent_core.loop_control import InjectItems, LoopDecision, NoAction
from openai_utils.model import SystemMessage

logger = logging.getLogger(__name__)


class TokenBudgetState(Enum):
    MONITORING = auto()
    WARNING_50 = auto()
    WARNING_90 = auto()
    FORCING_SUBMIT = auto()


class TokenBudgetHandler(BaseHandler):
    def __init__(self, max_tokens: int):
        self._max_tokens = max_tokens
        self._cumulative_tokens = 0
        self._state = TokenBudgetState.MONITORING
        self._last_was_reasoning = False

    @property
    def cumulative_tokens(self) -> int:
        return self._cumulative_tokens

    @property
    def state(self) -> TokenBudgetState:
        return self._state

    @property
    def percentage_used(self) -> float:
        return self._cumulative_tokens / self._max_tokens

    def on_reasoning(self, item: ReasoningItem) -> None:
        self._last_was_reasoning = True

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._last_was_reasoning = False

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._last_was_reasoning = False

    def on_response(self, evt: Response) -> None:
        if evt.usage.total_tokens:
            self._cumulative_tokens += evt.usage.total_tokens
            logger.info(
                "Tokens=%d/%d (%.1%%), state=%s",
                self._cumulative_tokens,
                self._max_tokens,
                self.percentage_used,
                self._state.name,
            )

    def on_before_sample(self) -> LoopDecision:
        pct = self.percentage_used

        if self._last_was_reasoning:
            return NoAction()

        if pct >= 1.0:
            if self._state != TokenBudgetState.FORCING_SUBMIT:
                logger.warning(
                    "BUDGET EXHAUSTED (%d/%d tokens), forcing submission", self._cumulative_tokens, self._max_tokens
                )
                self._state = TokenBudgetState.FORCING_SUBMIT

            return InjectItems(
                items=[
                    SystemMessage.text(
                        "TOKEN BUDGET EXHAUSTED. You MUST call submit_prompt immediately "
                        "with your current best prompt. The agent will be terminated if you "
                        "do not submit within the next turn."
                    )
                ]
            )

        if pct >= 0.9 and self._state == TokenBudgetState.WARNING_50:
            self._state = TokenBudgetState.WARNING_90
            return InjectItems(
                items=[
                    SystemMessage.text(
                        f"TOKEN BUDGET WARNING: 90% consumed ({self._cumulative_tokens:,}/{self._max_tokens:,} tokens). "
                        "Prioritize submitting your improved prompt soon. You have approximately "
                        f"{self._max_tokens - self._cumulative_tokens:,} tokens remaining."
                    )
                ]
            )

        if pct >= 0.5 and self._state == TokenBudgetState.MONITORING:
            self._state = TokenBudgetState.WARNING_50
            return InjectItems(
                items=[
                    SystemMessage.text(
                        f"TOKEN BUDGET NOTICE: 50% consumed ({self._cumulative_tokens:,}/{self._max_tokens:,} tokens). "
                        "You have approximately half your budget remaining. Plan your remaining work accordingly."
                    )
                ]
            )

        return NoAction()
