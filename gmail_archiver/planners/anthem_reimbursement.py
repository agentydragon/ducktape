"""Planner for archiving old Anthem reimbursement emails."""

import contextlib
import re
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email
from gmail_archiver.plan import Plan
from gmail_archiver.planners.age_archive import plan_age_archive


class AnthemReimbursement(BaseModel):
    patient_name: str | None = None
    care_date: datetime | None = None
    amount_you_pay: Decimal | None = None
    amount_deposited: Decimal | None = None
    claim_number_suffix: str | None = None
    email_date: datetime | None = None


def parse_anthem_reimbursement(email: Email) -> AnthemReimbursement:
    # Get email date from parsed Date header
    email_dt = email.date
    if email_dt:
        email_dt = email_dt.replace(tzinfo=None)

    # Extract text using get_text() which handles HTML conversion
    text = email.get_text()

    # Extract structured data using regex
    patient_name = m.group(1) if (m := re.search(r"Patient name:\s*([A-Z]+)", text)) else None
    claim_number_suffix = m.group(1) if (m := re.search(r"Claim number:\s*Ending in\s*(\d+)", text)) else None

    # Parse care date (MM/DD/YY format)
    care_dt = None
    if m := re.search(r"Date of care:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2})", text):
        with contextlib.suppress(ValueError):
            care_dt = datetime.strptime(m.group(1), "%m/%d/%y")

    # Parse amounts (remove commas from numbers like "8,676.65")
    amount_you_pay_decimal = None
    if m := re.search(r"Amount you pay:\s*\$([0-9,]+\.[0-9]{2})", text):
        with contextlib.suppress(ValueError, TypeError):
            amount_you_pay_decimal = Decimal(m.group(1).replace(",", ""))

    amount_deposited_decimal = None
    if m := re.search(r"Amount we deposited:\s*\$([0-9,]+\.[0-9]{2})", text):
        with contextlib.suppress(ValueError, TypeError):
            amount_deposited_decimal = Decimal(m.group(1).replace(",", ""))

    return AnthemReimbursement(
        patient_name=patient_name,
        care_date=care_dt,
        amount_you_pay=amount_you_pay_decimal,
        amount_deposited=amount_deposited_decimal,
        claim_number_suffix=claim_number_suffix,
        email_date=email_dt,
    )


class AnthemReimbursementPlanner:
    """Archives Anthem reimbursements older than 30 days."""

    name = "Anthem reimbursements"
    DAYS_THRESHOLD = 30

    def plan(self, inbox: GmailInbox) -> Plan:
        return plan_age_archive(
            inbox,
            planner=self,
            query="label:insurance/anthem/reimbursement label:INBOX",
            days_threshold=self.DAYS_THRESHOLD,
            parse=parse_anthem_reimbursement,
            date_from=lambda parsed: parsed.email_date,
            missing_date_reason="No email date found",
            archive_item_plural="reimbursements",
        )
