"""Test the grader tools that read reported issues back to the agent."""

from __future__ import annotations

from uuid import uuid4

import pytest_bazel

from props.agents.grader.main import GraderState, _create_grader_tools
from props.agents.grader.tools import IssueDetails
from props.db.models import AgentRunStatus, ReportedIssueOccurrence
from props.db.snapshots import LocationAnchor
from props.testing.fixtures.runs import make_fake_critic_run, make_reported_issues

# gazelle:include_dep //testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
# gazelle:include_dep //props/testing/fixtures:ground_truth
# gazelle:include_dep //props/testing/fixtures:runs
pytest_plugins = (
    "testing.postgres_fixtures",
    "props.testing.fixtures.db",
    "props.testing.fixtures.ground_truth",
    "props.testing.fixtures.runs",
)


async def test_show_issue_returns_every_occurrence_of_the_issue(synced_db, synced_test_session, example_subtract_orm):
    critic_run = make_fake_critic_run(
        session=synced_test_session, example=example_subtract_orm.to_example_spec(), status=AgentRunStatus.EXITED
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()
    run_id = critic_run.agent_run_id
    issue_id = "two-occurrence-issue"
    # make_reported_issues adds one occurrence, at subtract.py lines 1-1.
    make_reported_issues(agent_run_id=run_id, issue_ids=[issue_id], session=synced_test_session)
    synced_test_session.add(
        ReportedIssueOccurrence(
            agent_run_id=run_id,
            reported_issue_id=issue_id,
            locations=[LocationAnchor(file="add.py", start_line=3, end_line=4)],
        )
    )
    synced_test_session.commit()

    slug = example_subtract_orm.snapshot_slug
    tools = {tool.name: tool for tool in _create_grader_tools(uuid4(), slug, GraderState(slug), synced_db)}
    details = await tools["show_issue"].func(run=run_id, issue_id=issue_id)

    assert isinstance(details, IssueDetails)
    assert details.critique_run_id == run_id
    assert details.rationale == f"Test issue {issue_id}"
    locations = sorted((loc.file, loc.start_line, loc.end_line) for occ in details.occurrences for loc in occ.locations)
    assert locations == [("add.py", 3, 4), ("subtract.py", 1, 1)]


if __name__ == "__main__":
    pytest_bazel.main()
