"""Planner for archiving old DBSA SF event reminder emails."""

import asyncio
from datetime import datetime

from gmail_archiver.event_classifier import DBSASFGroupReminderData, EmailTemplateExtractor
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


def parse_event_datetime(email: Email, extractor: EmailTemplateExtractor) -> datetime | None:
    # Run async extraction in sync context
    extraction = asyncio.run(extractor.extract(email, use_cache=True))
    return extraction.data.event_datetime if isinstance(extraction.data, DBSASFGroupReminderData) else None


class DbsaEventPlanner:
    """Archives DBSA SF event reminders older than 1 day."""

    name = "DBSA SF events"
    DAYS_THRESHOLD = 1

    def __init__(self, extractor: EmailTemplateExtractor) -> None:
        self._extractor = extractor

    def plan(self, inbox: GmailInbox) -> Plan:
        return plan_age_archive(
            inbox,
            planner=self,
            query="label:events/dbsa-sf label:INBOX",
            days_threshold=self.DAYS_THRESHOLD,
            date_of=lambda message: parse_event_datetime(message, self._extractor),
            missing_date_reason="No event date found",
            archive_item_plural="event reminders",
        )
