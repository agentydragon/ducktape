"""Planner for archiving old DBSA SF event reminder emails."""

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from gmail_archiver.event_classifier import EmailTemplateExtractor
from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import LABEL_AUTO_CLEANED, Plan


class DBSASFEvent(BaseModel):
    event_datetime: datetime | None = None
    confidence: float = 0.0


def parse_dbsa_sf(email: Email, extractor: EmailTemplateExtractor) -> DBSASFEvent:
    async def extract_async():
        return await extractor.extract(email, use_cache=True)

    # Run async extraction in sync context
    extraction = asyncio.run(extract_async())

    # Extract event datetime if this is a DBSA SF reminder
    if extraction.data.template == "dbsa_sf_group_reminder" and extraction.data.event_datetime:
        return DBSASFEvent(event_datetime=extraction.data.event_datetime, confidence=extraction.confidence)

    # Unknown template or no datetime
    return DBSASFEvent(event_datetime=None, confidence=extraction.confidence)


class DbsaEventPlanner:
    """Archives DBSA SF event reminders older than 1 day."""

    name = "DBSA SF events"
    DAYS_THRESHOLD = 1

    def __init__(self, extractor: EmailTemplateExtractor) -> None:
        self._extractor = extractor

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        # Fetch messages with events/dbsa-sf label that are in inbox
        messages = inbox.fetch_messages("label:events/dbsa-sf label:INBOX")

        cutoff_date = datetime.now(UTC) - timedelta(days=self.DAYS_THRESHOLD)

        for message in messages:
            parsed = parse_dbsa_sf(message, self._extractor)

            if not parsed.event_datetime:
                plan.add_action(message=message, labels_to_add=[], labels_to_remove=[], reason="No event date found")
                continue

            # Make date timezone-aware for comparison
            event_date = parsed.event_datetime
            if event_date.tzinfo is None:
                event_date = event_date.replace(tzinfo=UTC)

            if event_date >= cutoff_date:
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
        plan.add_message(f"Archiving {archive_count} old event reminders (> {self.DAYS_THRESHOLD} days)")
        return plan
