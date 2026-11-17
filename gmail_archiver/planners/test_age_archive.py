"""Tests for age-based archive planning."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from gmail_api.labels import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import LABEL_AUTO_CLEANED, Planner
from gmail_archiver.planners import age_archive


def test_plan_age_archive_preserves_date_and_label_decisions(make_email, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2025, 1, 10, tzinfo=UTC)
    cutoff = now - timedelta(days=7)
    monkeypatch.setattr(age_archive, "datetime", SimpleNamespace(now=lambda _: now))

    emails = [
        make_email(id="missing", label_ids=[SystemLabel.INBOX]),
        make_email(id="at-cutoff", label_ids=[SystemLabel.INBOX]),
        make_email(id="naive-old", label_ids=[SystemLabel.INBOX]),
        make_email(id="already-labeled-old", label_ids=[SystemLabel.INBOX, LABEL_AUTO_CLEANED]),
    ]
    dates = {
        "missing": None,
        "at-cutoff": cutoff,
        "naive-old": (cutoff - timedelta(microseconds=1)).replace(tzinfo=None),
        "already-labeled-old": cutoff - timedelta(seconds=1),
    }

    def fetch_messages(query: str) -> list[Email]:
        assert query == "label:receipts/test label:INBOX"
        return emails

    plan = age_archive.plan_age_archive(
        cast(GmailInbox, SimpleNamespace(fetch_messages=fetch_messages)),
        planner=cast(Planner, SimpleNamespace(name="test archive planner")),
        query="label:receipts/test label:INBOX",
        days_threshold=7,
        parse=lambda email: email.id,
        date_from=dates.__getitem__,
        missing_date_reason="No date found",
        archive_item_plural="receipts",
    )

    assert plan.actions["missing"].action.reason == "No date found"
    assert not plan.actions["missing"].action.labels_to_add
    assert not plan.actions["missing"].action.labels_to_remove
    assert plan.actions["at-cutoff"].action.reason == "Too recent (within 7 days)"
    assert not plan.actions["at-cutoff"].action.labels_to_add
    assert not plan.actions["at-cutoff"].action.labels_to_remove
    assert plan.actions["naive-old"].action.reason == "Old enough (> 7 days)"
    assert plan.actions["naive-old"].action.labels_to_add == {LABEL_AUTO_CLEANED}
    assert plan.actions["naive-old"].action.labels_to_remove == {SystemLabel.INBOX}
    assert not plan.actions["already-labeled-old"].action.labels_to_add
    assert plan.actions["already-labeled-old"].action.labels_to_remove == {SystemLabel.INBOX}
    assert plan.messages == ["Archiving 2 old receipts (> 7 days)"]
