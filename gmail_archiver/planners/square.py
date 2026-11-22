"""Planner for archiving small Square receipt emails."""

import re
from decimal import Decimal

from pydantic import BaseModel

from gmail_api.labels import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import LABEL_AUTO_CLEANED, Plan


class SquareReceipt(BaseModel):
    merchant_name: str | None = None
    amount: Decimal | None = None

    @classmethod
    def display_columns(cls) -> list[tuple[str, str]]:
        return [("amount", "Amount"), ("merchant_name", "Merchant")]

    @classmethod
    def hide_subject(cls) -> bool:
        return False

    def format_column(self, key: str) -> str:
        if key == "amount":
            return f"${self.amount}" if self.amount else ""
        if key == "merchant_name":
            return self.merchant_name or ""
        return ""


# "You paid $27.78 with your Visa ending in 0000 to Test Merchant on Mar 16 2023 at 10:26 AM."
_PAYMENT_PATTERN = re.compile(r"You paid \$([0-9,]+\.[0-9]{2}) with your \w+ ending in \d{4} to (.+?) on ")


def parse_square(email: Email) -> SquareReceipt:
    if not (match := _PAYMENT_PATTERN.search(email.get_text())):
        return SquareReceipt()
    return SquareReceipt(merchant_name=match.group(2), amount=Decimal(match.group(1).replace(",", "")))


class SquarePlanner:
    """Archives Square receipts under $30."""

    name = "Square receipts"
    THRESHOLD = Decimal("30.00")

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

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
