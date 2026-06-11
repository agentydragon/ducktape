"""Drift detection handler for snapshot grader daemon.

Checks grading_pending before each sample, aborts when no drift (grading complete).
Drains notification queue and injects context about GT changes during work.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from agent_core.handler import BaseHandler
from agent_core.loop_control import Abort, InjectItems, LoopDecision, NoAction
from openai_utils.model import UserMessage
from props.core.db.session import get_session

logger = logging.getLogger(__name__)


def check_grading_pending(snapshot_slug: str) -> bool:
    """Check if there's pending grading work for the snapshot."""
    with get_session() as session:
        result = session.execute(
            text("SELECT 1 FROM grading_pending WHERE snapshot_slug = :slug LIMIT 1"), {"slug": snapshot_slug}
        )
        return result.scalar() is not None


def format_notifications(notifs: list[dict]) -> str:
    """Format notification payloads for injection into agent context."""
    if not notifs:
        return ""
    lines = ["GT changes detected:"]
    for n in notifs:
        event = n.get("event", "unknown")
        # e.g., "INSERT_true_positives", "DELETE_false_positive_occurrences"
        lines.append(f"  - {event}")
    return "\n".join(lines)


class GraderDriftHandler(BaseHandler):
    """Handler that checks for grading drift and controls daemon sleep/wake.

    Behaviors:
    - Checks grading_pending before each sample (source of truth for drift)
    - Drains notification queue to prevent buildup
    - Injects notification content as context when new events arrive during work
    - Returns Abort() when drift is empty (signals scaffold to sleep)

    The scaffold awaits pg_notify after Abort(), then injects wake message
    and calls run() again.
    """

    def __init__(self, snapshot_slug: str, notification_queue: list[dict], wake_event: asyncio.Event):
        """Initialize drift handler.

        Args:
            snapshot_slug: Snapshot this daemon is responsible for
            notification_queue: Shared queue that scaffold's listener appends to
            wake_event: Event set by listener when notifications arrive
        """
        self._snapshot_slug = snapshot_slug
        self._queue = notification_queue
        self._wake_event = wake_event

    def on_before_sample(self) -> LoopDecision:
        """Check drift status and decide whether to continue, inject, or abort."""
        # Drain any notifications that arrived while we were working
        notifs = list(self._queue)
        self._queue.clear()
        self._wake_event.clear()

        # Check actual drift from database
        has_drift = check_grading_pending(self._snapshot_slug)

        if not has_drift:
            logger.info(f"No drift for {self._snapshot_slug}, daemon going to sleep")
            return Abort()

        # If we have notifications to report, inject them as context
        if notifs:
            msg = format_notifications(notifs)
            logger.debug(f"Injecting {len(notifs)} notifications for {self._snapshot_slug}")
            return InjectItems(items=[UserMessage.text(msg)])

        return NoAction()
