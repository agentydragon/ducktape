"""Planner for archiving old Anthem EOB emails."""

from datetime import datetime

from pydantic import BaseModel

from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


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
        return plan_age_archive(
            inbox,
            planner=self,
            query="label:insurance/anthem/eob label:INBOX",
            days_threshold=self.DAYS_THRESHOLD,
            parse=parse_anthem_eob,
            date_from=lambda parsed: parsed.received_date,
            missing_date_reason="No received date found",
            archive_item_plural="EOBs",
        )
