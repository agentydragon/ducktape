import pytest_bazel

from finance.plaid.db.products import Product, syncable_products


def test_syncable_products_intersects_and_orders() -> None:
    assert syncable_products(["liabilities", "auth", "investments", "transactions"]) == [
        Product.TRANSACTIONS,
        Product.INVESTMENTS,
        Product.LIABILITIES,
    ]


def test_an_institution_offering_none_of_them_yields_nothing() -> None:
    """Requesting a product the institution lacks fails the whole Link, so no fallback product."""
    assert syncable_products(["auth", "identity"]) == []


if __name__ == "__main__":
    pytest_bazel.main()
