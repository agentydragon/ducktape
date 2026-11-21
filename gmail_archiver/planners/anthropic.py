"""Planner for archiving old Anthropic receipt emails."""

import re
from datetime import datetime

import dateutil.parser
from pydantic import BaseModel

from gmail_archiver.date_patterns import MONTHS
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


class AnthropicReceipt(BaseModel):
    charge_date: datetime | None = None


_DATE_REGEX = re.compile(rf"Paid\s+({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}})", re.IGNORECASE)


def parse_anthropic(email: Email) -> AnthropicReceipt:
    body = email.get_text()

    # Extract charge date: "Paid December 13, 2025"
    charge_date = None
    if date_match := _DATE_REGEX.search(body):
        # Match groups: (month, day, year)
        date_str = f"{date_match.group(1)} {date_match.group(2)}, {date_match.group(3)}"
        charge_date = dateutil.parser.parse(date_str)  # Let exceptions propagate

    return AnthropicReceipt(charge_date=charge_date)


class AnthropicReceiptPlanner:
    """Archives Anthropic receipts older than 30 days."""

    name = "Anthropic receipts"
    DAYS_THRESHOLD = 30

    def plan(self, inbox: GmailInbox) -> Plan:
        return plan_age_archive(
            inbox,
            planner=self,
            query="label:receipts/anthropic label:INBOX",
            days_threshold=self.DAYS_THRESHOLD,
            parse=parse_anthropic,
            date_from=lambda parsed: parsed.charge_date,
            missing_date_reason="No charge date found",
            archive_item_plural="receipts",
        )
