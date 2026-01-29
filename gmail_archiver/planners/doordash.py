"""Planner for managing DoorDash order emails."""

import re
from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel

from fmt_util.fmt_util import format_truncation_suffix
from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan

LABEL_DOORDASH_AUTO_CLEANED = "gmail-archiver/doordash-auto-cleaned"


class DoorDashEmailType(StrEnum):
    CONFIRMATION = "confirmation"
    DELIVERY = "delivery"
    RECEIPT = "receipt"
    UNKNOWN = "unknown"


# Email type priority - higher = more final
EMAIL_TYPE_PRIORITY = {
    DoorDashEmailType.CONFIRMATION: 1,
    DoorDashEmailType.DELIVERY: 2,
    DoorDashEmailType.RECEIPT: 3,
    DoorDashEmailType.UNKNOWN: 0,
}


class DoorDashEmail(BaseModel):
    """Parsed DoorDash email data."""

    restaurant: str | None
    email_type: DoorDashEmailType

    @classmethod
    def display_columns(cls) -> list[tuple[str, str]]:
        return [("restaurant", "Restaurant"), ("email_type", "Type")]

    def format_column(self, key: str) -> str:
        if key == "restaurant":
            return self.restaurant or ""
        if key == "email_type":
            return self.email_type.value
        return ""


class DoorDashParseError(Exception):
    """Raised when a DoorDash email cannot be parsed."""


def parse_doordash_subject(subject: str) -> DoorDashEmail:
    """Parse restaurant name and email type from DoorDash email subject.

    Patterns:
    - "Order Confirmation for Michael from Sardine Can"
    - "Details of your no-contact delivery from Sardine Can"
    - "Final receipt for Michael from Target"
    """
    # Order confirmation
    if m := re.search(r"Order Confirmation for \w+ from (.+)$", subject):
        return DoorDashEmail(restaurant=m.group(1), email_type=DoorDashEmailType.CONFIRMATION)

    # Delivery notification
    if m := re.search(r"delivery from (.+)$", subject):
        return DoorDashEmail(restaurant=m.group(1), email_type=DoorDashEmailType.DELIVERY)

    # Final receipt
    if m := re.search(r"receipt for \w+ from (.+)$", subject):
        return DoorDashEmail(restaurant=m.group(1), email_type=DoorDashEmailType.RECEIPT)

    raise DoorDashParseError(f"Unrecognized DoorDash email subject: {subject}")


class DoorDashPlanner:
    """Manages DoorDash order emails.

    Groups emails by (restaurant, date) and keeps only the most recent/final email
    per order. The progression is typically: confirmation → delivery → receipt.

    Since DoorDash doesn't include order IDs in emails, we use restaurant + date
    as a proxy for order identity.
    """

    name = "DoorDash orders"

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        messages = inbox.fetch_messages("from:doordash.com label:INBOX")

        # Group by (restaurant, date)
        by_order: dict[tuple[str, str], list[Email]] = defaultdict(list)
        parsed_cache: dict[str, DoorDashEmail] = {}
        unparseable: list[Email] = []

        for msg in messages:
            try:
                parsed = parse_doordash_subject(msg.subject)
                parsed_cache[msg.id] = parsed

                if parsed.restaurant:
                    # Use date (YYYY-MM-DD) as part of the key
                    date_key = msg.internal_date.strftime("%Y-%m-%d")
                    order_key = (parsed.restaurant, date_key)
                    by_order[order_key].append(msg)
                else:
                    unparseable.append(msg)
            except DoorDashParseError:
                unparseable.append(msg)

        if unparseable:
            plan.add_message(f"Skipping {len(unparseable)} emails with unrecognized format:")
            for msg in unparseable[:5]:
                plan.add_message(f"  - {msg.subject[:60]}...")
            if suffix := format_truncation_suffix(len(unparseable), 5):
                plan.add_message(suffix)

        for order_emails in by_order.values():
            # Sort by email type priority (highest priority = most final)
            # Within same priority, sort by date (newest first)
            sorted_emails = sorted(
                order_emails,
                key=lambda m: (EMAIL_TYPE_PRIORITY.get(parsed_cache[m.id].email_type, 0), m.internal_date),
                reverse=True,
            )

            latest = sorted_emails[0]
            latest_parsed = parsed_cache[latest.id]

            # Keep the most final email
            plan.add_action(
                message=latest,
                labels_to_add=[],
                labels_to_remove=[],
                reason=f"Latest ({latest_parsed.email_type})",
                custom_data=latest_parsed,
            )

            # Archive older/less final emails
            for msg in sorted_emails[1:]:
                plan.add_action(
                    message=msg,
                    labels_to_add=[LABEL_DOORDASH_AUTO_CLEANED],
                    labels_to_remove=[SystemLabel.INBOX],
                    reason="Superseded by later email",
                    custom_data=parsed_cache[msg.id],
                )

        return plan
