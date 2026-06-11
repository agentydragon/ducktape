"""Planner for archiving small Square receipt emails."""

import contextlib
import re
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import LABEL_AUTO_CLEANED, Plan


class SquareReceipt(BaseModel):
    merchant_name: str | None = None
    amount: Decimal | None = None
    card_type: str | None = None
    card_last4: str | None = None
    transaction_datetime: datetime | None = None
    email_date: datetime | None = None

    @classmethod
    def display_columns(cls) -> list[tuple[str, str]]:
        return [("amount", "Amount"), ("merchant_name", "Merchant")]

    def format_column(self, key: str) -> str:
        if key == "amount":
            return f"${self.amount}" if self.amount else ""
        if key == "merchant_name":
            return self.merchant_name or ""
        return ""


def parse_square(email: Email) -> SquareReceipt:
    # Get email date from parsed Date header
    email_dt = email.date
    if email_dt:
        email_dt = email_dt.replace(tzinfo=None)

    # Extract text using get_text() which handles HTML conversion
    text = email.get_text()

    # Extract structured data using regex
    # Pattern: "You paid $27.78 with your Visa ending in 6915 to Bean Scene Cafe on Mar 16 2023 at 10:26 AM."
    # Captures datetime up to period or end of string
    amount_decimal = None
    card_type = None
    card_last4 = None
    merchant_name = None
    transaction_dt = None

    if m := re.search(
        r"You paid \$([0-9,]+\.[0-9]{2}) with your (\w+) ending in (\d{4}) to (.+?) on ([^.]+?)(?:\.|$)", text
    ):
        with contextlib.suppress(ValueError, TypeError):
            amount_decimal = Decimal(m.group(1).replace(",", ""))

        card_type = m.group(2)
        card_last4 = m.group(3)
        merchant_name = m.group(4)

        # Parse transaction datetime - expected format: "Mar 16 2023 at 10:26 AM"
        with contextlib.suppress(ValueError):
            transaction_dt = datetime.strptime(m.group(5).strip(), "%b %d %Y at %I:%M %p")

    return SquareReceipt(
        merchant_name=merchant_name,
        amount=amount_decimal,
        card_type=card_type,
        card_last4=card_last4,
        transaction_datetime=transaction_dt,
        email_date=email_dt,
    )


class SquarePlanner:
    """Archives Square receipts under $30."""

    name = "Square receipts"
    THRESHOLD = Decimal("30.00")

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        # Fetch messages using inbox interface
        query = "from:receipts@messaging.squareup.com in:inbox"
        messages = inbox.fetch_messages(query)

        for message in messages:
            parsed = parse_square(message)

            if not parsed.amount:
                plan.add_action(
                    message=message, labels_to_add=[], labels_to_remove=[], reason="No amount found", custom_data=parsed
                )
                continue

            if parsed.amount < self.THRESHOLD:
                plan.add_action(
                    message=message,
                    labels_to_add=[LABEL_AUTO_CLEANED],
                    labels_to_remove=[SystemLabel.INBOX],
                    reason=f"Amount < ${self.THRESHOLD}",
                    custom_data=parsed,
                )
            else:
                plan.add_action(
                    message=message,
                    labels_to_add=[],
                    labels_to_remove=[],
                    reason=f"Amount >= ${self.THRESHOLD}",
                    custom_data=parsed,
                )

        return plan
