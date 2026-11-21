"""Planner for archiving old Anthem EOB emails."""

from gmail_archiver.inbox import GmailInbox
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


class AnthemEobPlanner:
    """Archives Anthem EOBs older than 180 days: they are actionable for that long (dispute window)."""

    name = "Anthem EOBs"
    DAYS_THRESHOLD = 180

    def plan(self, inbox: GmailInbox) -> Plan:
        return plan_age_archive(
            inbox,
            planner=self,
            query="label:insurance/anthem/eob label:INBOX",
            days_threshold=self.DAYS_THRESHOLD,
            date_of=lambda email: email.date,
            missing_date_reason="No received date found",
            archive_item_plural="EOBs",
        )
