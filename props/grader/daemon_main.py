"""Grader daemon main entry point for in-container execution.

This is the CMD entrypoint for the daemon grader container. It:
1. Fetches the snapshot to /workspace
2. Runs the reconciliation loop:
   - Grade until grading_pending is empty
   - Sleep waiting for pg_notify
   - Wake and repeat
3. Only exits on fatal error or shutdown signal
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from props.core.agent_helpers import fetch_snapshot, get_current_agent_run
from props.core.ids import SnapshotSlug
from props.core.loop_utils import WORKSPACE, render_system_prompt, setup_logging
from props.db.config import get_database_config
from props.db.session import get_session
from props.grader.drift_handler import check_grading_pending
from props.grader.loop import GraderMode, run_grader_loop
from props.grader.notifications import GRADING_PENDING_CHANNEL, GradingPendingNotification

logger = logging.getLogger(__name__)


class DaemonState:
    """Tracks daemon state for pg_notify wake/sleep coordination."""

    def __init__(self, snapshot_slug: SnapshotSlug):
        self.snapshot_slug = snapshot_slug
        self.wake_event = asyncio.Event()
        self.shutdown = False
        self.notification_queue: list[GradingPendingNotification] = []

    def notification_callback(
        self, connection: asyncpg.Connection[Any] | PoolConnectionProxy[Any], pid: int, channel: str, payload: object
    ) -> None:
        """Handle incoming pg_notify notifications."""
        if not isinstance(payload, str):
            raise TypeError(f"Expected string payload, got {type(payload)}")

        notification = GradingPendingNotification.model_validate_json(payload)

        if notification.snapshot_slug != self.snapshot_slug:
            return  # Not for us

        logger.debug(f"Notification for {self.snapshot_slug}: {notification.operation} {notification.item.table}")
        self.notification_queue.append(notification)
        self.wake_event.set()


async def run_daemon(snapshot_slug: SnapshotSlug, model: str, system_prompt: str) -> int:
    """Run the daemon reconciliation loop.

    Grades until no drift, sleeps on pg_notify, repeats.
    """
    state = DaemonState(snapshot_slug)
    db_config = get_database_config()

    # Start pg_notify listener
    listener_conn = await asyncpg.connect(db_config.admin.url())
    await listener_conn.add_listener(GRADING_PENDING_CHANNEL, state.notification_callback)
    logger.info(f"Listening on channel '{GRADING_PENDING_CHANNEL}' for {snapshot_slug}")

    try:
        while not state.shutdown:
            # Check if there's drift to process
            if not check_grading_pending(snapshot_slug):
                # No drift, wait for notification
                logger.info(f"No drift for {snapshot_slug}, sleeping...")
                state.wake_event.clear()
                state.notification_queue.clear()
                await state.wake_event.wait()

                if state.shutdown:
                    break

                logger.info(f"Woken by {len(state.notification_queue)} notifications")
                continue

            # There's drift, run agent loop
            logger.info("Drift detected, starting agent loop")
            exit_code = await run_grader_loop(system_prompt, model, snapshot_slug, GraderMode.DAEMON)

            if exit_code != 0:
                # Agent reported failure
                logger.error("Agent loop failed with exit code %d", exit_code)
                state.shutdown = True

            # Loop continues - check for more drift

    finally:
        await listener_conn.remove_listener(GRADING_PENDING_CHANNEL, state.notification_callback)
        await listener_conn.close()
        logger.info("Listener stopped")

    return 0 if not state.shutdown else 1


async def main() -> int:
    """Main entry point for daemon grader agent."""
    setup_logging()

    logger.info("Grader daemon starting")

    # Get snapshot and model from agent run config
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        config = agent_run.grader_config()
        snapshot_slug = SnapshotSlug(config.snapshot_slug)
        model = agent_run.model
        logger.info("Agent run: %s, snapshot: %s, model: %s", agent_run.agent_run_id, snapshot_slug, model)

    # Fetch snapshot
    logger.info("Fetching snapshot to %s", WORKSPACE)
    fetch_snapshot(WORKSPACE)

    # Render system prompt
    logger.info("Rendering system prompt")
    system_prompt = render_system_prompt("props/docs/agents/grader.md.j2")

    # Run the daemon loop
    logger.info("Starting daemon loop")
    exit_code = await run_daemon(snapshot_slug, model, system_prompt)

    logger.info("Daemon exited with code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
