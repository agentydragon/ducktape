"""Tests for Anthropic receipt parser."""

from datetime import datetime
from decimal import Decimal

import pytest_bazel

from gmail_archiver.planners.anthropic import AnthropicReceipt, parse_anthropic

SAMPLE_RECEIPT_FULL = """
Anthropic, PBC

Receipt from Anthropic, PBC $90.28 Paid December 15, 2025

Receipt number 2554-1935-9612
Invoice number OKBBHMMB-0145
Payment method - 5474

Auto-recharge credits Qty 1 $90.28
Total $90.28
Amount paid $90.28
"""

SAMPLE_RECEIPT_MINIMAL = """
Anthropic, PBC

Receipt from Anthropic, PBC $45.00 Paid January 1, 2025

Some other text here without receipt or invoice numbers.
"""


class TestAnthropicParser:
    def test_parse_full_receipt(self, make_email):
        msg = make_email(
            sender="invoice+statements@mail.anthropic.com",
            subject="Your receipt from Anthropic, PBC #2554-1935-9612",
            body=SAMPLE_RECEIPT_FULL,
        )

        receipt = parse_anthropic(msg)

        assert isinstance(receipt, AnthropicReceipt)
        assert receipt.amount == Decimal("90.28")
        assert receipt.charge_date == datetime(2025, 12, 15)
        assert receipt.invoice_number == "OKBBHMMB-0145"
        assert receipt.receipt_number == "2554-1935-9612"

    def test_parse_minimal_receipt(self, make_email):
        msg = make_email(sender="invoice+statements@mail.anthropic.com", body=SAMPLE_RECEIPT_MINIMAL)

        receipt = parse_anthropic(msg)

        assert isinstance(receipt, AnthropicReceipt)
        assert receipt.amount == Decimal("45.00")
        assert receipt.charge_date == datetime(2025, 1, 1)
        assert receipt.invoice_number is None
        assert receipt.receipt_number is None

    def test_parse_missing_all_fields(self, make_email):
        msg = make_email(sender="invoice+statements@mail.anthropic.com", body="This email has no extractable data.")

        receipt = parse_anthropic(msg)

        assert isinstance(receipt, AnthropicReceipt)
        assert receipt.amount is None
        assert receipt.charge_date is None
        assert receipt.invoice_number is None
        assert receipt.receipt_number is None

    def test_parse_multiple_amounts_uses_first(self, make_email):
        msg = make_email(
            sender="invoice+statements@mail.anthropic.com",
            body="""
            Receipt from Anthropic, PBC $10.00 Paid January 1, 2025
            Some other price $20.00 mentioned here.
            """,
        )

        receipt = parse_anthropic(msg)
        assert receipt.amount == Decimal("10.00")

    def test_parse_number_fields_accept_their_shortest_shapes(self, make_email):
        """Shortest ids the invoice and receipt patterns accept."""
        msg = make_email(
            sender="invoice+statements@mail.anthropic.com", body="Invoice number A-1\nReceipt number 0-0-0"
        )

        receipt = parse_anthropic(msg)
        assert receipt.invoice_number == "A-1"
        assert receipt.receipt_number == "0-0-0"


if __name__ == "__main__":
    pytest_bazel.main()
