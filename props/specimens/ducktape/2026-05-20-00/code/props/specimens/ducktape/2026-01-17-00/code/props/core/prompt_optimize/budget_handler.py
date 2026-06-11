"""Budget enforcement handler for prompt optimization runs.

Monitors cumulative costs across critic/grader runs and enforces budget limits by:
1. Checking if budget is exhausted after each tool result
2. When budget reached: inject final system message and switch to text-only mode
3. Agent produces summary report (detected via on_assistant_text_event)
4. Abort on next sample
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session

from agent_core.events import AssistantText
from agent_core.handler import BaseHandler
from agent_core.loop_control import Abort, ForbidAllTools, InjectItems, LoopDecision, NoAction
from openai_utils.model import UserMessage
from props.core.db import query_builders as qb
from props.core.db.session import get_session

if TYPE_CHECKING:
    from agent_core.agent import Agent

logger = logging.getLogger(__name__)


class BudgetState(StrEnum):
    """Budget enforcement state machine states."""

    MONITORING = "monitoring"
    SUMMARY_REQUESTED = "summary_requested"
    SUMMARY_PRODUCED = "summary_produced"


class BudgetEnforcementHandler(BaseHandler):
    """Enforce budget limits for prompt optimization runs.

    Tracks cumulative costs across all critic/grader runs linked to a PO run ID.
    When budget is reached:
    1. Inject system message requesting final summary report
    2. Switch agent to text-only mode (ForbidAllTools)
    3. Allow agent one final turn to produce report
    4. Abort on next sample attempt

    State machine:
    - MONITORING: normal operation, checking budget before each sample
    - SUMMARY_REQUESTED: budget exceeded, injected summary request, waiting for final text response
    - SUMMARY_PRODUCED: got final response, ready to abort
    """

    def __init__(
        self,
        *,
        optimizer_run_id: UUID,
        budget_limit: float,  # USD
        agent: Agent,
    ) -> None:
        self._optimizer_run_id = optimizer_run_id
        self._budget_limit = budget_limit
        self._agent = agent
        self._state = BudgetState.MONITORING

    def _query_total_cost(self, session: Session) -> float:
        result = session.execute(qb.po_run_costs(self._optimizer_run_id)).fetchall()
        return sum((row.cost_usd for row in result if row.cost_usd is not None), start=0.0)

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        if self._state == BudgetState.SUMMARY_REQUESTED:
            self._state = BudgetState.SUMMARY_PRODUCED
            logger.info(f"PO run {self._optimizer_run_id}: Summary report produced, will abort on next sample")

    def on_before_sample(self) -> LoopDecision:
        """Enforce budget limits before each sampling step.

        State transitions:
        1. SUMMARY_PRODUCED → Abort
        2. MONITORING with budget exceeded → inject summary request, transition to SUMMARY_REQUESTED
        3. SUMMARY_REQUESTED → NoAction (waiting for response)
        4. MONITORING with budget OK → NoAction
        """
        if self._state == BudgetState.SUMMARY_PRODUCED:
            logger.info(f"PO run {self._optimizer_run_id}: Aborting after summary")
            return Abort()

        if self._state == BudgetState.MONITORING:
            with get_session() as session:
                cumulative_cost = self._query_total_cost(session)

            if cumulative_cost >= self._budget_limit:
                logger.info(
                    f"PO run {self._optimizer_run_id}: Budget exhausted (${cumulative_cost:.4f} >= ${self._budget_limit:.2f})"
                )
                self._state = BudgetState.SUMMARY_REQUESTED
                self._agent._tool_policy = ForbidAllTools()
                logger.info(f"PO run {self._optimizer_run_id}: Switched to text-only mode (ForbidAllTools)")

                summary_request = UserMessage.text(
                    f"""\
Your budget of ${self._budget_limit:.2f} has been exceeded.
Tool calls are now disabled. Produce a final summary report with:

1. **Best prompt found**: prompt SHA256 and key insights
2. **Performance summary**: best recall achieved on valid split
3. **Key learnings**: what worked, what didn't, patterns discovered
4. **Recommendations**: next steps for further optimization

Make this your final response - the session will end after this message.
"""
                )

                return InjectItems(items=[summary_request])

        return NoAction()
