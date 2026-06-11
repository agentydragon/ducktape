"""Tests for Anthropic receipt parser."""

from datetime import datetime
from decimal import Decimal

import pytest

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

    @pytest.mark.parametrize(
        ("date_str", "expected_date"),
        [
            ("Paid January 15, 2025", datetime(2025, 1, 15)),
            ("Paid February 1, 2025", datetime(2025, 2, 1)),
            ("Paid December 31, 2024", datetime(2024, 12, 31)),
        ],
    )
    def test_parse_date_formats(self, make_email, date_str, expected_date):
        msg = make_email(
            sender="invoice+statements@mail.anthropic.com", body=f"Receipt from Anthropic, PBC $10.00 {date_str}"
        )

        receipt = parse_anthropic(msg)
        assert receipt.charge_date == expected_date

    def test_parse_invalid_date_returns_none(self, make_email):
        msg = make_email(sender="invoice+statements@mail.anthropic.com", body="Paid InvalidMonth 32, 2025")

        receipt = parse_anthropic(msg)
        assert receipt.charge_date is None

    @pytest.mark.parametrize(
        ("text", "expected_invoice"),
        [
            ("Invoice number OKBBHMMB-0145", "OKBBHMMB-0145"),
            ("Invoice number ABC123XY-9999", "ABC123XY-9999"),
            ("Invoice number A-1", "A-1"),
        ],
    )
    def test_parse_invoice_number_format(self, make_email, text, expected_invoice):
        msg = make_email(sender="invoice+statements@mail.anthropic.com", body=text)

        receipt = parse_anthropic(msg)
        assert receipt.invoice_number == expected_invoice

    @pytest.mark.parametrize(
        ("text", "expected_receipt"),
        [
            ("Receipt number 2554-1935-9612", "2554-1935-9612"),
            ("Receipt number 1234-5678-9012", "1234-5678-9012"),
            ("Receipt number 0-0-0", "0-0-0"),
        ],
    )
    def test_parse_receipt_number_format(self, make_email, text, expected_receipt):
        msg = make_email(sender="invoice+statements@mail.anthropic.com", body=text)

        receipt = parse_anthropic(msg)
        assert receipt.receipt_number == expected_receipt
