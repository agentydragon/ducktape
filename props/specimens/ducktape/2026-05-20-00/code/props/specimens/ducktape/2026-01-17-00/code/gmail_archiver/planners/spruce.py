"""Planner for archiving Spruce notification emails.

Keeps only the most recent notification in inbox (if < 30 days old).
Archives all older notifications.
"""

from datetime import UTC, datetime, timedelta

from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.plan import LABEL_AUTO_CLEANED, Plan


class SprucePlanner:
    """Archives Spruce notifications, keeping only the latest one."""

    name = "Spruce notifications"
    DAYS_THRESHOLD = 30

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        messages = inbox.fetch_messages('from:noreply@sprucehealth.com subject:"[Spruce] New activity for" in:inbox')
        if not messages:
            return plan

        # Sort by internal_date descending (newest first)
        messages.sort(key=lambda m: m.internal_date, reverse=True)

        cutoff = datetime.now(UTC) - timedelta(days=self.DAYS_THRESHOLD)
        newest = messages[0]

        # Keep newest only if it's recent enough
        if newest.internal_date >= cutoff:
            plan.add_action(
                message=newest, labels_to_add=[], labels_to_remove=[], reason="Most recent, keeping in inbox"
            )
            to_archive = messages[1:]
        else:
            # Even the newest is too old, archive everything
            to_archive = messages

        for message in to_archive:
            plan.add_action(
                message=message,
                labels_to_add=[LABEL_AUTO_CLEANED],
                labels_to_remove=[SystemLabel.INBOX],
                reason="Older notification",
            )

        plan.add_message(f"Keeping 1 newest, archiving {len(to_archive)} older notifications")
        return plan
