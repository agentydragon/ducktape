"""Planner for archiving old Anthropic receipt emails."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import dateutil.parser
from pydantic import BaseModel

from gmail_archiver.date_patterns import MONTHS
from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import LABEL_AUTO_CLEANED, Plan


class AnthropicReceipt(BaseModel):
    amount: Decimal | None = None
    charge_date: datetime | None = None
    invoice_number: str | None = None
    receipt_number: str | None = None


_DATE_REGEX = re.compile(rf"Paid\s+({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}})", re.IGNORECASE)
_AMOUNT_REGEX = re.compile(r"\$(\d+\.\d{2})")
_INVOICE_REGEX = re.compile(r"Invoice number ([A-Z0-9-]+)")
_RECEIPT_REGEX = re.compile(r"Receipt number ([0-9-]+)")


def parse_anthropic(email: Email) -> AnthropicReceipt:
    body = email.get_text()

    # Extract charge date: "Paid December 13, 2025"
    charge_date = None
    if date_match := _DATE_REGEX.search(body):
        # Match groups: (month, day, year)
        date_str = f"{date_match.group(1)} {date_match.group(2)}, {date_match.group(3)}"
        charge_date = dateutil.parser.parse(date_str)  # Let exceptions propagate

    # Extract amount: "$90.19"
    amount = Decimal(amount_match.group(1)) if (amount_match := _AMOUNT_REGEX.search(body)) else None

    # Extract invoice number: "OKBBHMMB-0070"
    invoice_number = invoice_match.group(1) if (invoice_match := _INVOICE_REGEX.search(body)) else None

    # Extract receipt number: "2281-0248-1919"
    receipt_number = receipt_match.group(1) if (receipt_match := _RECEIPT_REGEX.search(body)) else None

    return AnthropicReceipt(
        amount=amount, charge_date=charge_date, invoice_number=invoice_number, receipt_number=receipt_number
    )


class AnthropicReceiptPlanner:
    """Archives Anthropic receipts older than 30 days."""

    name = "Anthropic receipts"
    DAYS_THRESHOLD = 30

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        # Fetch messages with receipts/anthropic label that are in inbox
        messages = inbox.fetch_messages("label:receipts/anthropic label:INBOX")

        cutoff_date = datetime.now(UTC) - timedelta(days=self.DAYS_THRESHOLD)

        for message in messages:
            parsed = parse_anthropic(message)

            if not parsed.charge_date:
                plan.add_action(message=message, labels_to_add=[], labels_to_remove=[], reason="No charge date found")
                continue

            # Make date timezone-aware for comparison
            charge_date = parsed.charge_date
            if charge_date.tzinfo is None:
                charge_date = charge_date.replace(tzinfo=UTC)

            if charge_date >= cutoff_date:
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
        plan.add_message(f"Archiving {archive_count} old receipts (> {self.DAYS_THRESHOLD} days)")
        return plan
