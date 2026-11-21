"""Plan archival for messages whose parsed date is past a retention window."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from gmail_api.labels import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import LABEL_AUTO_CLEANED, Plan, Planner


def plan_age_archive(
    inbox: GmailInbox,
    *,
    planner: Planner,
    query: str,
    days_threshold: int,
    date_of: Callable[[Email], datetime | None],
    missing_date_reason: str,
    archive_item_plural: str,
) -> Plan:
    plan = Plan(planner=planner)
    messages = inbox.fetch_messages(query)
    cutoff_date = datetime.now(UTC) - timedelta(days=days_threshold)

    for message in messages:
        message_date = date_of(message)
        if message_date is None:
            plan.add_action(message=message, reason=missing_date_reason)
            continue

        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=UTC)

        if message_date >= cutoff_date:
            plan.add_action(message=message, reason=f"Too recent (within {days_threshold} days)")
        else:
            plan.add_action(
                message=message,
                labels_to_add=[LABEL_AUTO_CLEANED],
                labels_to_remove=[SystemLabel.INBOX],
                reason=f"Old enough (> {days_threshold} days)",
            )

    archive_count = sum(1 for action in plan.actions.values() if SystemLabel.INBOX in action.action.labels_to_remove)
    plan.add_message(f"Archiving {archive_count} old {archive_item_plural} (> {days_threshold} days)")
    return plan
