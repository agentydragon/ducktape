"""Automatic transcript compaction handler for Agent agents.

Monitors cumulative token usage and triggers compaction when approaching limits.

TODO: Reconsider compaction flow design
---------------------------------------
Current flow is awkward with 2 decision points:
1. Handler returns Compact() decision in on_before_sample()
2. Agent maybe executes compaction (might skip if not enough items)
3. Handler gets callback in on_compaction_complete() with result

This creates a round-trip where the handler requests something but doesn't know if it will happen.

Alternative: Maybe the handler should just directly do the compaction and transcript replacement:
- Handler has direct access to agent's transcript
- Handler can check if there are enough items before attempting
- No round-trip callback needed
- Cleaner control flow

Trade-offs:
- Current: Agent owns transcript mutation (centralized), but handler doesn't know if request succeeds
- Direct: Handler has full control and immediate feedback, but needs access to agent internals
"""

from __future__ import annotations

import logging

from agent_core.events import AssistantText, ReasoningItem, Response, ToolCall
from agent_core.handler import BaseHandler
from agent_core.loop_control import Compact, LoopDecision, NoAction

logger = logging.getLogger(__name__)


class CompactionHandler(BaseHandler):
    """Automatically compact transcript when approaching token limits.

    Monitors cumulative token usage from API responses and triggers compaction
    when threshold is exceeded. Can compact multiple times during a long session.

    After each compaction request, resets the token counter to start fresh. If
    compaction is skipped (not enough transcript items), will keep requesting it
    every turn until sufficient items accumulate.

    Note: Will not trigger compaction if the last transcript item was a ReasoningItem,
    since reasoning blocks cannot be reused outside their original response context.
    """

    def __init__(self, threshold_tokens: int = 150_000, keep_recent_turns: int = 10):
        """Initialize compaction handler.

        Args:
            threshold_tokens: Token count threshold to trigger compaction (default 150k = 75% of 200k)
            keep_recent_turns: Number of recent transcript items to preserve (default 10)
        """
        self._threshold = threshold_tokens
        self._keep_recent = keep_recent_turns
        self._cumulative_tokens = 0
        self._last_item_was_reasoning = False
        logger.info(
            "CompactionHandler initialized: threshold=%d tokens, keep_recent=%d turns",
            threshold_tokens,
            keep_recent_turns,
        )

    def on_reasoning(self, item: ReasoningItem) -> None:
        """Track when reasoning items are added to transcript."""
        self._last_item_was_reasoning = True
        logger.debug(
            "CompactionHandler: reasoning item detected, compaction will be deferred until next non-reasoning response"
        )

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        """Clear reasoning flag when non-reasoning content is added."""
        # Once we see assistant text, the reasoning context is complete
        # and it's safe to compact after this turn
        if self._last_item_was_reasoning:
            logger.debug("CompactionHandler: assistant text after reasoning, compaction now safe")
        self._last_item_was_reasoning = False

    def on_tool_call_event(self, evt: ToolCall) -> None:
        """Clear reasoning flag when tool calls are added."""
        # Tool calls indicate we've moved past the reasoning phase
        if self._last_item_was_reasoning:
            logger.debug("CompactionHandler: tool calls after reasoning, compaction now safe")
        self._last_item_was_reasoning = False

    def on_response(self, evt: Response) -> None:
        """Track actual token usage from OpenAI API."""
        if evt.usage.total_tokens:
            self._cumulative_tokens += evt.usage.total_tokens
            percentage = 100 * self._cumulative_tokens / self._threshold
            logger.info(
                "CompactionHandler: tokens=%d/%d (%.1f%%)", self._cumulative_tokens, self._threshold, percentage
            )

    def on_before_sample(self) -> LoopDecision:
        """Return Compact decision when threshold exceeded.

        Will not trigger compaction if last item was a ReasoningItem,
        since reasoning cannot be reused outside its original response context.

        Token counter is reset in on_compaction_complete() only when compaction
        succeeds, allowing multiple compactions during a long session.
        """
        logger.debug(
            "CompactionHandler.on_before_sample: tokens=%d, threshold=%d, last_was_reasoning=%s",
            self._cumulative_tokens,
            self._threshold,
            self._last_item_was_reasoning,
        )

        if self._last_item_was_reasoning:
            logger.info(
                "CompactionHandler: compaction deferred (last item is ReasoningItem, threshold=%d, current=%d)",
                self._threshold,
                self._cumulative_tokens,
            )
            return NoAction()

        if self._cumulative_tokens > self._threshold:
            logger.info(
                "CompactionHandler: threshold exceeded (%d > %d), requesting compaction (keep_recent=%d)",
                self._cumulative_tokens,
                self._threshold,
                self._keep_recent,
            )
            # Counter will be reset in on_compaction_complete() if compaction succeeds
            return Compact(keep_recent_turns=self._keep_recent)

        logger.debug(
            "CompactionHandler: below threshold (%d <= %d), no compaction needed",
            self._cumulative_tokens,
            self._threshold,
        )
        return NoAction()  # Defer to other handlers

    def on_compaction_complete(self, compacted: bool) -> None:
        """Reset token counter after successful compaction.

        Only resets when compaction actually succeeds. If compaction is skipped
        (not enough transcript items), keeps the current token count so we'll
        keep requesting compaction until it succeeds.
        """
        if compacted:
            logger.info("CompactionHandler: compaction succeeded, resetting token counter to 0")
            self._cumulative_tokens = 0
        else:
            logger.info(
                "CompactionHandler: compaction skipped, keeping current token count (%d) to retry next turn",
                self._cumulative_tokens,
            )
