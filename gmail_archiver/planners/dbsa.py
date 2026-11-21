"""Planner for archiving old DBSA SF event reminder emails."""

import asyncio
from datetime import datetime

from pydantic import BaseModel

from gmail_archiver.event_classifier import EmailTemplateExtractor
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


class DBSASFEvent(BaseModel):
    event_datetime: datetime | None = None
    confidence: float = 0.0


def parse_dbsa_sf(email: Email, extractor: EmailTemplateExtractor) -> DBSASFEvent:
    async def extract_async():
        return await extractor.extract(email, use_cache=True)

    # Run async extraction in sync context
    extraction = asyncio.run(extract_async())

    if extraction.data.template == "dbsa_sf_group_reminder" and extraction.data.event_datetime:
        return DBSASFEvent(event_datetime=extraction.data.event_datetime, confidence=extraction.confidence)

    return DBSASFEvent(event_datetime=None, confidence=extraction.confidence)


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
            parse=lambda message: parse_dbsa_sf(message, self._extractor),
            date_from=lambda parsed: parsed.event_datetime,
            missing_date_reason="No event date found",
            archive_item_plural="event reminders",
        )
