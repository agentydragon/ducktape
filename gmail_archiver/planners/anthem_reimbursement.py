"""Planner for archiving old Anthem reimbursement emails."""

from datetime import datetime

from pydantic import BaseModel

from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


class AnthemReimbursement(BaseModel):
    email_date: datetime | None = None


def parse_anthem_reimbursement(email: Email) -> AnthemReimbursement:
    email_dt = email.date
    if email_dt:
        email_dt = email_dt.replace(tzinfo=None)
    return AnthemReimbursement(email_date=email_dt)


class AnthemReimbursementPlanner:
    """Archives Anthem reimbursements older than 30 days."""

    name = "Anthem reimbursements"
    DAYS_THRESHOLD = 30

    def plan(self, inbox: GmailInbox) -> Plan:
        return plan_age_archive(
            inbox,
            planner=self,
            query="label:insurance/anthem/reimbursement label:INBOX",
            days_threshold=self.DAYS_THRESHOLD,
            parse=parse_anthem_reimbursement,
            date_from=lambda parsed: parsed.email_date,
            missing_date_reason="No email date found",
            archive_item_plural="reimbursements",
        )
