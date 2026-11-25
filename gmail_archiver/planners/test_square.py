"""Tests for Square receipt parser."""

from decimal import Decimal

import pytest
import pytest_bazel

from gmail_archiver.planners.square import parse_square

FULL_RECEIPT = """
<html>
<body>
Reply to this email to leave feedback for Test Merchant
You paid $27.78 with your Visa ending in 0000 to Test Merchant on Mar 16 2023 at 10:26 AM.

View your full receipt:
https://example.test/receipt/TEST-RECEIPT-0001
</body>
</html>
"""


@pytest.mark.parametrize(
    ("body", "merchant_name", "amount"),
    [
        (FULL_RECEIPT, "Test Merchant", Decimal("27.78")),
        (
            "You paid $1,234.56 with your Visa ending in 5678 to Expensive Store on Jan 1 2024 at 1:00 PM.",
            "Expensive Store",
            Decimal("1234.56"),
        ),
        (
            "You paid $5.50 with your Visa ending in 1111 to Test Merchant's Tea & Co on Dec 25 2023 at 8:00 AM.",
            "Test Merchant's Tea & Co",
            Decimal("5.50"),
        ),
        ("This is an invalid receipt format.", None, None),
    ],
)
def test_parse_square(make_email, body: str, merchant_name: str | None, amount: Decimal | None) -> None:
    result = parse_square(make_email(sender="receipts@messaging.squareup.com", body=body))

    assert (result.merchant_name, result.amount) == (merchant_name, amount)


if __name__ == "__main__":
    pytest_bazel.main()
