"""Planner for archiving old Anthem reimbursement emails."""

from gmail_archiver.inbox import GmailInbox
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


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
            date_of=lambda email: email.date and email.date.replace(tzinfo=None),
            missing_date_reason="No email date found",
            archive_item_plural="reimbursements",
        )
