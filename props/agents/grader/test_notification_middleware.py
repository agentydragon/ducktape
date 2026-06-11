from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

import pytest_bazel
from agent_framework import ChatContext, Message

from props.agents.grader.notification_middleware import _format_notifications, notification_chat_middleware
from props.core.ids import SnapshotSlug
from props.db.notifications import GradingPendingNotification, Operation, ReportedIssuesItem, TruePositivesItem

_SLUG = SnapshotSlug("test/snapshot")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000001")


def _critique_notification() -> GradingPendingNotification:
    return GradingPendingNotification(
        operation=Operation.INSERT, item=ReportedIssuesItem(agent_run_id=_RUN_ID, issue_id="i1"), snapshot_slug=_SLUG
    )


def _tp_notification() -> GradingPendingNotification:
    return GradingPendingNotification(
        operation=Operation.INSERT, item=TruePositivesItem(tp_id="tp1"), snapshot_slug=_SLUG
    )


@dataclass
class _FakeChatContext:
    """Minimal stand-in: the middleware only reads/appends `messages`."""

    messages: list[Message] = field(default_factory=list)


async def _invoke(queue: list[GradingPendingNotification]) -> tuple[_FakeChatContext, bool]:
    ctx = _FakeChatContext()
    called = False

    async def call_next() -> None:
        nonlocal called
        called = True

    await notification_chat_middleware(queue)(cast(ChatContext, ctx), call_next)
    return ctx, called


def test_format_notifications_text() -> None:
    text = _format_notifications([_critique_notification(), _tp_notification()])
    assert "2 new grading notification(s)" in text
    assert "reported_issues" in text
    assert "true_positives" in text


def test_empty_queue_appends_nothing() -> None:
    ctx, called = asyncio.run(_invoke([]))
    assert ctx.messages == []
    assert called  # call_next still runs


def test_notifications_appended_and_drained() -> None:
    queue = [_critique_notification(), _tp_notification()]
    ctx, called = asyncio.run(_invoke(queue))
    assert len(ctx.messages) == 1
    assert "2 new grading notification(s)" in ctx.messages[0].text
    assert len(queue) == 0
    assert called


if __name__ == "__main__":
    pytest_bazel.main()
