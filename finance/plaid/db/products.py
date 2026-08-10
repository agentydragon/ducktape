"""Plaid products this app can sync.

Narrower than Plaid's full catalog: these are the three the sync path knows how to mirror. An
institution typically supports more, and the link UI shows the difference.
"""

from enum import StrEnum


class Product(StrEnum):
    TRANSACTIONS = "transactions"
    INVESTMENTS = "investments"
    LIABILITIES = "liabilities"


def syncable_products(institution_products: list[str]) -> list[Product]:
    """The institution's products intersected with what sync can mirror, in a stable order.

    Requesting a product the institution does not support fails the *whole* Link, so the
    intersection is what the UI offers rather than a fixed set chosen ahead of knowing the bank.
    """
    supported = set(institution_products)
    return [product for product in Product if product.value in supported]
