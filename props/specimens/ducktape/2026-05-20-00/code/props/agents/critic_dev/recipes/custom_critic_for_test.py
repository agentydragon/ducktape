"""Minimal custom critic entry point for E2E testing.

Bypasses the LLM agent loop entirely: connects to DB, inserts one
reported issue + occurrence, and exits. This creates grading drift
that the snapshot grader picks up.
"""

from __future__ import annotations

import asyncio
import sys

from props.agents.runtime import get_current_agent_run_id
from props.db.database import Database
from props.db.models import ReportedIssue, ReportedIssueOccurrence
from props.db.snapshots import LocationAnchor


async def main() -> int:
    db = Database.from_env()

    with db.session() as session:
        agent_run_id = get_current_agent_run_id(session)
        print(f"Custom critic running as {agent_run_id}")
        session.add(
            ReportedIssue(
                agent_run_id=agent_run_id,
                issue_id="build-script-test-issue",
                rationale="Test issue from build_critic.sh custom image",
            )
        )

    # Separate session: occurrence references the issue via FK, so the issue
    # must be committed first (db.session() auto-commits on exit).
    with db.session() as session:
        session.add(
            ReportedIssueOccurrence(
                agent_run_id=agent_run_id,
                reported_issue_id="build-script-test-issue",
                locations=[LocationAnchor(file="test.py", start_line=1, end_line=5)],
            )
        )

    print("Custom critic completed: 1 issue, 1 occurrence")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
