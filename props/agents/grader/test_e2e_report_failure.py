"""E2E test for grader report_failure tool.

Verifies that when the LLM calls report_failure:
1. The grader container exits with code 1 (failure)
2. The AgentRun record is marked EXITED with container_exit_code=1
3. The mock script is fully consumed — report_failure is the last scripted
   response, so no further LLM calls are made (AbortIf fires first)
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import pytest
import pytest_bazel

from agent_core.testing.responses import PlayGen
from props.agents.grader.testing.mocks import GraderMock
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, ReportedIssue, ReportedIssueOccurrence
from props.db.snapshots import LocationAnchor
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import make_fake_critic_run

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


@pytest.mark.timeout(120)
async def test_grader_report_failure_exits_with_failure(
    e2e_stack, test_snapshot, all_files_scope, grader_image, db: Database
):
    """report_failure causes grader to exit with code 1.

    The mock yields one response (the report_failure tool call). The agent
    processes it (state.failed=True), then AbortIf fires before the next LLM
    call — so the mock generator is left paused at its last yield rather than
    completed. We track that the tool was actually reached via an asyncio.Event.
    """
    report_failure_called = asyncio.Event()

    # check_consumed=False: report_failure terminates the agent before a second
    # API call, so the generator never advances past its last yield.
    @GraderMock.mock(check_consumed=False)
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request (system prompt)
        report_failure_called.set()
        yield m.report_failure("Cannot grade: injected test failure")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[grader_image]) as stack:
        critic_run_id = uuid4()
        with db.session() as session:
            critic_run = make_fake_critic_run(
                session=session,
                example=all_files_scope,
                model=stack.model,
                status=AgentRunStatus.EXITED,
                agent_run_id=critic_run_id,
            )
            session.add(critic_run)
            session.flush()
            session.add(
                ReportedIssue(
                    agent_run_id=critic_run_id, issue_id="fail-issue-1", rationale="Issue to trigger grader then fail"
                )
            )
            session.add(
                ReportedIssueOccurrence(
                    agent_run_id=critic_run_id,
                    reported_issue_id="fail-issue-1",
                    locations=[LocationAnchor(file="subtract.py", start_line=1, end_line=1)],
                )
            )
            session.commit()

        grader_handle = await stack.registry.start_snapshot_grader(
            image=stack.resolved_images["grader"], snapshot_slug=test_snapshot, model=stack.model
        )

        # Grader should exit quickly once report_failure is processed
        status = await asyncio.wait_for(grader_handle, timeout=90)

        assert report_failure_called.is_set(), "report_failure tool call was never reached"
        assert status == AgentRunStatus.EXITED

        with db.session() as session:
            grader_run = session.get(AgentRun, grader_handle.agent_run_id)
            assert grader_run is not None, "No grader AgentRun found in DB"
            assert grader_run.status == AgentRunStatus.EXITED
            assert grader_run.container_exit_code == 1, (
                f"Expected exit code 1 (failure), got {grader_run.container_exit_code}"
            )


if __name__ == "__main__":
    pytest_bazel.main()
