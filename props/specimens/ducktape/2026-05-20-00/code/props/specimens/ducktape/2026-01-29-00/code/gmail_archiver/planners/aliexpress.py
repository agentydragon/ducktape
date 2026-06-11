"""Planner for managing AliExpress order notification emails."""

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel

from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan

LABEL_ALIEXPRESS_AUTO_CLEANED = "gmail-archiver/aliexpress-auto-cleaned"


class AliExpressStatus(StrEnum):
    """Status of an AliExpress order based on email subject."""

    CONFIRMED = "confirmed"
    READY_TO_SHIP = "ready_to_ship"
    SHIPPED = "shipped"
    IN_TRANSIT = "in_transit"
    CLEARED_CUSTOMS = "cleared_customs"
    IN_COUNTRY = "in_country"
    AT_DELIVERY_CENTER = "at_delivery_center"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    DELIVERY_UPDATE = "delivery_update"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    FEEDBACK_REQUEST = "feedback_request"
    CLOSED = "closed"
    DELAYED_COUPON = "delayed_coupon"


STATES_WITH_DEADLINE = {AliExpressStatus.AWAITING_CONFIRMATION, AliExpressStatus.FEEDBACK_REQUEST}


class AliExpressEmail(BaseModel):
    """Parsed AliExpress email data."""

    order_id: str | None
    status: AliExpressStatus
    confirmation_deadline: datetime | None = None

    @classmethod
    def display_columns(cls) -> list[tuple[str, str]]:
        return [("order_id", "Order ID"), ("status", "Status")]

    @classmethod
    def hide_subject(cls) -> bool:
        # Order ID + Status fully capture AliExpress subject content
        return True

    def format_column(self, key: str) -> str:
        if key == "order_id":
            return self.order_id or ""
        if key == "status":
            return self.status.value
        return ""


# Regex patterns for subject parsing
ORDER_ID_PATTERN = re.compile(r"Order (\d+):")
STATUS_PATTERNS = [
    ("delivered", AliExpressStatus.DELIVERED),
    ("out for delivery", AliExpressStatus.OUT_FOR_DELIVERY),
    ("at delivery center", AliExpressStatus.AT_DELIVERY_CENTER),
    ("in your country", AliExpressStatus.IN_COUNTRY),
    ("cleared customs", AliExpressStatus.CLEARED_CUSTOMS),
    ("package in transit", AliExpressStatus.IN_TRANSIT),
    ("order shipped", AliExpressStatus.SHIPPED),
    ("ready to ship", AliExpressStatus.READY_TO_SHIP),
    ("order confirmed", AliExpressStatus.CONFIRMED),
    ("delivery update", AliExpressStatus.DELIVERY_UPDATE),
    ("awaiting confirmation", AliExpressStatus.AWAITING_CONFIRMATION),
    ("how did it go", AliExpressStatus.FEEDBACK_REQUEST),
    ("is closed", AliExpressStatus.CLOSED),
    ("delayed delivery coupon", AliExpressStatus.DELAYED_COUPON),
]


class AliExpressParseError(Exception):
    """Raised when an AliExpress email cannot be parsed."""


def parse_aliexpress_subject(subject: str) -> AliExpressEmail:
    """Parse order ID and status from AliExpress email subject.

    Raises AliExpressParseError if the status cannot be determined.
    """
    # Extract order ID
    order_match = ORDER_ID_PATTERN.search(subject)
    order_id = order_match.group(1) if order_match else None

    # Determine status
    subject_lower = subject.lower()
    for pattern_text, status in STATUS_PATTERNS:
        if pattern_text in subject_lower:
            return AliExpressEmail(order_id=order_id, status=status)

    raise AliExpressParseError(f"Unrecognized AliExpress email subject: {subject}")


DISPUTE_WINDOW_DAYS = 15  # AliExpress allows 15 days after confirmation to dispute

# Pattern for "Delivered DD/MM/YYYY" in email body
DELIVERED_DATE_PATTERN = re.compile(r"Delivered\s+(\d{1,2})/(\d{1,2})/(\d{4})")


def extract_delivered_date(body: str) -> datetime | None:
    """Extract delivered date from AliExpress email body."""
    if match := DELIVERED_DATE_PATTERN.search(body):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            return datetime(year, month, day)
        except ValueError:
            return None
    return None


def compute_deadline(message: Email) -> datetime | None:
    """Compute deadline from delivered date + 15 days.

    Falls back to email received date + 30 days if delivered date not found.
    TODO: Would be nice to match with actual delivery dates from other emails in the order.
    """
    if delivered := extract_delivered_date(message.get_text()):
        return delivered + timedelta(days=DISPUTE_WINDOW_DAYS)

    # Fallback: internal_date + 30 days
    return message.internal_date.replace(tzinfo=None) + timedelta(days=30)


def parse_aliexpress(message: Email, should_compute_deadline: bool = False) -> AliExpressEmail:
    """Parse AliExpress email, optionally computing deadline from received date."""
    parsed = parse_aliexpress_subject(message.subject)

    if should_compute_deadline and parsed.status in STATES_WITH_DEADLINE:
        parsed.confirmation_deadline = compute_deadline(message)

    return parsed


class AliExpressPlanner:
    """Manages AliExpress order notification emails.

    Logic:
    - Groups emails by order ID
    - For terminal states (delivered, closed, feedback_request): archive all
    - For awaiting_confirmation: keep if deadline not passed, else archive
    - For in-progress states: keep only latest per order, archive older
    """

    name = "AliExpress orders"

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        # Fetch AliExpress transaction emails in inbox (specific sender, not all aliexpress)
        messages = inbox.fetch_messages("from:transaction@notice.aliexpress.com label:INBOX")

        # Group by order ID
        by_order: dict[str | None, list[Email]] = defaultdict(list)
        parsed_cache: dict[str, AliExpressEmail] = {}
        unparseable: list[Email] = []

        for msg in messages:
            try:
                parsed = parse_aliexpress_subject(msg.subject)
                parsed_cache[msg.id] = parsed
                by_order[parsed.order_id].append(msg)
            except AliExpressParseError:
                unparseable.append(msg)

        now = datetime.now(UTC)

        # Report unparseable emails but don't take action
        if unparseable:
            plan.add_message(f"Skipping {len(unparseable)} emails with unrecognized status:")
            for msg in unparseable:
                plan.add_message(f"  - {msg.subject[:60]}...")

        for order_emails in by_order.values():
            # Sort by date, newest first
            sorted_emails = sorted(order_emails, key=lambda m: m.internal_date, reverse=True)
            latest = sorted_emails[0]
            latest_parsed = parsed_cache[latest.id]

            # For states with deadlines, check if deadline passed → archive all
            if latest_parsed.status in STATES_WITH_DEADLINE:
                full_parsed = parse_aliexpress(latest, should_compute_deadline=True)

                if full_parsed.confirmation_deadline and full_parsed.confirmation_deadline < now:
                    # Deadline passed - archive all emails for this order
                    for msg in sorted_emails:
                        plan.add_action(
                            message=msg,
                            labels_to_add=[LABEL_ALIEXPRESS_AUTO_CLEANED],
                            labels_to_remove=[SystemLabel.INBOX],
                            reason=f"Deadline passed: {full_parsed.confirmation_deadline}",
                            custom_data=parsed_cache[msg.id],
                        )
                    continue

            # Default: keep only latest, archive older
            plan.add_action(
                message=latest,
                labels_to_add=[],
                labels_to_remove=[],
                reason=f"Latest ({latest_parsed.status})",
                custom_data=latest_parsed,
            )

            for msg in sorted_emails[1:]:
                plan.add_action(
                    message=msg,
                    labels_to_add=[LABEL_ALIEXPRESS_AUTO_CLEANED],
                    labels_to_remove=[SystemLabel.INBOX],
                    reason="Older (keeping latest only)",
                    custom_data=parsed_cache[msg.id],
                )

        return plan
