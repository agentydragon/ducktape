"""Tests for Square receipt parser."""

from datetime import datetime
from decimal import Decimal

import pytest_bazel

from gmail_archiver.planners.square import SquareReceipt, parse_square


class TestSquareReceiptParser:
    def test_parse_full_receipt(self, make_email):
        email = make_email(
            subject="Receipt from Bean Scene Cafe",
            sender="Bean Scene Cafe via Square <receipts@messaging.squareup.com>",
            date="Thu, 16 Mar 2023 17:31:44 +0000",
            body="""
            <html>
            <body>
            Reply to this email to leave feedback for Bean Scene Cafe
            You paid $27.78 with your Visa ending in 6915 to Bean Scene Cafe on Mar 16 2023 at 10:26 AM.

            View your full receipt:
            https://squareup.com/r/ar3BboQHLr6qVtG9OOIn7BTEzeuaB
            </body>
            </html>
            """,
        )

        result = parse_square(email)

        assert isinstance(result, SquareReceipt)
        assert result.merchant_name == "Bean Scene Cafe"
        assert result.amount == Decimal("27.78")
        assert result.card_type == "Visa"
        assert result.card_last4 == "6915"
        assert result.transaction_datetime == datetime(2023, 3, 16, 10, 26)
        assert result.email_date == datetime(2023, 3, 16, 17, 31, 44)

    def test_parse_different_card_type(self, make_email):
        email = make_email(
            sender="receipts@messaging.squareup.com",
            body="You paid $50.00 with your Mastercard ending in 1234 to Test Store on Jan 15 2024 at 2:30 PM.",
        )

        result = parse_square(email)

        assert result.card_type == "Mastercard"
        assert result.card_last4 == "1234"
        assert result.merchant_name == "Test Store"

    def test_parse_amount_with_commas(self, make_email):
        email = make_email(
            sender="receipts@messaging.squareup.com",
            body="You paid $1,234.56 with your Visa ending in 5678 to Expensive Store on Jan 1 2024 at 1:00 PM.",
        )

        result = parse_square(email)

        assert result.amount == Decimal("1234.56")

    def test_parse_missing_payment_info(self, make_email):
        email = make_email(sender="receipts@messaging.squareup.com", body="This is an invalid receipt format.")

        result = parse_square(email)

        assert result.merchant_name is None
        assert result.amount is None
        assert result.card_type is None
        assert result.card_last4 is None
        assert result.transaction_datetime is None

    def test_parse_merchant_name_with_special_chars(self, make_email):
        email = make_email(
            sender="receipts@messaging.squareup.com",
            body="You paid $5.50 with your Visa ending in 1111 to Joe's Coffee & Tea on Dec 25 2023 at 8:00 AM.",
        )

        result = parse_square(email)

        assert result.merchant_name == "Joe's Coffee & Tea"

    def test_parse_invalid_email_date(self, make_email):
        email = make_email(
            sender="receipts@messaging.squareup.com",
            date="Invalid date format",
            body="You paid $10.00 with your Visa ending in 1234 to Test Store on Jan 1 2024 at 12:00 PM.",
        )

        result = parse_square(email)

        assert result.email_date is None
        assert result.amount == Decimal("10.00")

    def test_parse_invalid_transaction_datetime(self, make_email):
        email = make_email(
            sender="receipts@messaging.squareup.com",
            body="You paid $10.00 with your Visa ending in 1234 to Test Store on Invalid Date.",
        )

        result = parse_square(email)

        assert result.transaction_datetime is None
        assert result.amount == Decimal("10.00")


if __name__ == "__main__":
    pytest_bazel.main()
