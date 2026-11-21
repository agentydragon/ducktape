"""Tests for Anthropic receipt parser."""

from datetime import datetime

import pytest
import pytest_bazel

from gmail_archiver.planners.anthropic import parse_anthropic

SAMPLE_RECEIPT_FULL = """
Anthropic, PBC

Receipt from Anthropic, PBC $90.28 Paid December 15, 2025

Receipt number 0000-0000-0000
Invoice number TEST-INVOICE-0001
Payment method - 0000

Auto-recharge credits Qty 1 $90.28
Total $90.28
Amount paid $90.28
"""

SAMPLE_RECEIPT_MINIMAL = """
Anthropic, PBC

Receipt from Anthropic, PBC $45.00 Paid January 1, 2025

Some other text here without receipt or invoice numbers.
"""


@pytest.mark.parametrize(
    ("text", "charge_date"),
    [
        (SAMPLE_RECEIPT_FULL, datetime(2025, 12, 15)),
        (SAMPLE_RECEIPT_MINIMAL, datetime(2025, 1, 1)),
        ("This email has no extractable data.", None),
    ],
)
def test_parse_charge_date(make_email, text: str, charge_date: datetime | None) -> None:
    msg = make_email(sender="invoice+statements@mail.anthropic.com", body=text)

    assert parse_anthropic(msg).charge_date == charge_date


if __name__ == "__main__":
    pytest_bazel.main()
