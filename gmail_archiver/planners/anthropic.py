"""Planner for archiving old Anthropic receipt emails."""

import re
from datetime import datetime

import dateutil.parser

from gmail_archiver.date_patterns import MONTHS
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive

_DATE_REGEX = re.compile(rf"Paid\s+({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}})", re.IGNORECASE)


def parse_charge_date(email: Email) -> datetime | None:
    """Parse "Paid December 13, 2025" from the body."""
    if not (match := _DATE_REGEX.search(email.get_text())):
        return None
    month, day, year = match.groups()
    return dateutil.parser.parse(f"{month} {day}, {year}")  # Let exceptions propagate


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
            date_of=parse_charge_date,
            missing_date_reason="No charge date found",
            archive_item_plural="receipts",
        )
