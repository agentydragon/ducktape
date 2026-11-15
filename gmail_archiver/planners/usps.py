"""Planner for archiving old USPS Informed Delivery emails."""

import re
from datetime import datetime

from pydantic import BaseModel

from gmail_archiver.date_patterns import DAYS_OF_WEEK, MONTHS
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


class USPSDelivery(BaseModel):
    expected_delivery_date: datetime | None = None


# Subject patterns:
#   - "USPS® Expected Delivery by Thursday, December 18, 2025 arriving by 9:00pm"
#   - "USPS® Expected Delivery on Monday, December 15, 2025 arriving by 7:20pm"
_DATE_PATTERN = re.compile(rf"(?:by|on)\s+({DAYS_OF_WEEK},\s+{MONTHS}\s+\d+,\s+\d{{4}})", re.IGNORECASE)


def parse_usps(email: Email) -> USPSDelivery | None:
    if not email.subject:
        return None

    match = _DATE_PATTERN.search(email.subject)
    if not match:
        return None

    date_str = match.group(1)  # e.g., "Thursday, December 18, 2025"

    try:
        expected_delivery_date = datetime.strptime(date_str, "%A, %B %d, %Y")
    except ValueError:
        return None

    return USPSDelivery(expected_delivery_date=expected_delivery_date)


class UspsPlanner:
    """Archives USPS Informed Delivery emails older than 7 days."""

    name = "USPS Informed Delivery"
    DAYS_THRESHOLD = 7

    def plan(self, inbox: GmailInbox) -> Plan:
        return plan_age_archive(
            inbox,
            planner=self,
            query="label:batch/usps-informed-delivery label:INBOX",
            days_threshold=self.DAYS_THRESHOLD,
            parse=parse_usps,
            date_from=lambda parsed: parsed.expected_delivery_date if parsed is not None else None,
            missing_date_reason="Could not parse delivery date",
            archive_item_plural="deliveries",
        )
