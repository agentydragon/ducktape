"""Planner for archiving old Anthem EOB emails."""

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import LABEL_AUTO_CLEANED, Plan


class AnthemEOB(BaseModel):
    """EOBs are actionable for 180 days (dispute window), then safe to archive."""

    received_date: datetime | None = None


def parse_anthem_eob(email: Email) -> AnthemEOB:
    email_dt = email.date
    if email_dt:
        email_dt = email_dt.replace(tzinfo=None)
    return AnthemEOB(received_date=email_dt)


class AnthemEobPlanner:
    """Archives Anthem EOBs older than 180 days."""

    name = "Anthem EOBs"
    DAYS_THRESHOLD = 180

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        # Fetch messages with insurance/anthem/eob label that are in inbox
        messages = inbox.fetch_messages("label:insurance/anthem/eob label:INBOX")

        cutoff_date = datetime.now(UTC) - timedelta(days=self.DAYS_THRESHOLD)

        for message in messages:
            parsed = parse_anthem_eob(message)

            if not parsed.received_date:
                plan.add_action(message=message, labels_to_add=[], labels_to_remove=[], reason="No received date found")
                continue

            # Make date timezone-aware for comparison
            received_date = parsed.received_date
            if received_date.tzinfo is None:
                received_date = received_date.replace(tzinfo=UTC)

            if received_date >= cutoff_date:
                plan.add_action(
                    message=message,
                    labels_to_add=[],
                    labels_to_remove=[],
                    reason=f"Too recent (within {self.DAYS_THRESHOLD} days)",
                )
            else:
                plan.add_action(
                    message=message,
                    labels_to_add=[LABEL_AUTO_CLEANED],
                    labels_to_remove=[SystemLabel.INBOX],
                    reason=f"Old enough (> {self.DAYS_THRESHOLD} days)",
                )

        # Count actual archive operations
        archive_count = sum(1 for pa in plan.actions.values() if SystemLabel.INBOX in pa.action.labels_to_remove)
        plan.add_message(f"Archiving {archive_count} old EOBs (> {self.DAYS_THRESHOLD} days)")
        return plan
