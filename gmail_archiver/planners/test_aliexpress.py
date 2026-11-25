"""Tests for the AliExpress order planner."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
import pytest_bazel

from gmail_api.labels import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.planners.aliexpress import LABEL_ALIEXPRESS_AUTO_CLEANED, AliExpressPlanner

DEADLINE_SUBJECTS = ["Order 8001: Awaiting confirmation", "Order 8001: How did it go?"]


def _received(days_ago: int) -> int:
    return int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp() * 1000)


def _plan(emails: list[Email]):
    return AliExpressPlanner().plan(cast(GmailInbox, SimpleNamespace(fetch_messages=lambda _query: emails)))


def _inbox_email(make_email, **fields):
    return make_email(label_ids=[SystemLabel.INBOX], **fields)


@pytest.mark.parametrize("subject", DEADLINE_SUBJECTS)
def test_order_is_archived_once_the_deadline_after_the_latest_email_passed(make_email, subject: str) -> None:
    plan = _plan(
        [
            _inbox_email(
                make_email, id="transit", subject="Order 8001: Package in transit", internal_date=_received(61)
            ),
            _inbox_email(make_email, id="latest", subject=subject, internal_date=_received(60)),
        ]
    )

    for message_id in ("transit", "latest"):
        action = plan.actions[message_id].action
        assert action.labels_to_add == {LABEL_ALIEXPRESS_AUTO_CLEANED}
        assert action.labels_to_remove == {SystemLabel.INBOX}


@pytest.mark.parametrize("subject", DEADLINE_SUBJECTS)
def test_order_keeps_only_its_latest_email_before_the_deadline(make_email, subject: str) -> None:
    plan = _plan(
        [
            _inbox_email(
                make_email, id="transit", subject="Order 8001: Package in transit", internal_date=_received(2)
            ),
            _inbox_email(make_email, id="latest", subject=subject, internal_date=_received(1)),
        ]
    )

    assert not plan.actions["latest"].action.labels_to_remove
    assert plan.actions["transit"].action.labels_to_remove == {SystemLabel.INBOX}


def test_delivered_date_in_the_body_sets_the_deadline(make_email) -> None:
    plan = _plan(
        [
            _inbox_email(
                make_email,
                id="latest",
                subject=DEADLINE_SUBJECTS[0],
                body="Delivered 01/01/2020",
                internal_date=_received(1),
            )
        ]
    )

    assert plan.actions["latest"].action.labels_to_remove == {SystemLabel.INBOX}


if __name__ == "__main__":
    pytest_bazel.main()
