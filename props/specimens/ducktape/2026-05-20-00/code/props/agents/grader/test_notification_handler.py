from __future__ import annotations

from uuid import UUID

import pytest_bazel

from agent_core.loop_control import InjectItems, NoAction
from openai_utils.text_extraction import extract_input_text_content
from props.agents.grader.notification_handler import GraderNotificationsHandler
from props.core.ids import SnapshotSlug
from props.db.notifications import GradingPendingNotification, Operation, ReportedIssuesItem, TruePositivesItem

_SLUG = SnapshotSlug("test/snapshot")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000001")


def _make_critique_notification() -> GradingPendingNotification:
    return GradingPendingNotification(
        operation=Operation.INSERT, item=ReportedIssuesItem(agent_run_id=_RUN_ID, issue_id="i1"), snapshot_slug=_SLUG
    )


def _make_tp_notification() -> GradingPendingNotification:
    return GradingPendingNotification(
        operation=Operation.INSERT, item=TruePositivesItem(tp_id="tp1"), snapshot_slug=_SLUG
    )


def test_empty_queue_returns_no_action():
    queue: list[GradingPendingNotification] = []
    handler = GraderNotificationsHandler(queue)
    assert isinstance(handler.on_before_sample(), NoAction)


def test_notifications_injected_as_user_message():
    queue = [_make_critique_notification(), _make_tp_notification()]
    handler = GraderNotificationsHandler(queue)

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    assert len(decision.items) == 1

    texts = extract_input_text_content(list(decision.items))
    assert texts
    text = texts[0]
    assert "2 new grading notification(s)" in text
    assert "reported_issues" in text
    assert "true_positives" in text


def test_queue_drained_after_injection():
    queue = [_make_critique_notification()]
    handler = GraderNotificationsHandler(queue)

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)

    # Queue should be empty now
    assert len(queue) == 0
    # Second call returns NoAction
    assert isinstance(handler.on_before_sample(), NoAction)


if __name__ == "__main__":
    pytest_bazel.main()
