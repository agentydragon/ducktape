import pytest_bazel

from finance.plaid.db.products import Product, syncable_products


def test_syncable_products_intersects_and_orders() -> None:
    assert syncable_products(["liabilities", "auth", "investments", "transactions"]) == [
        Product.TRANSACTIONS,
        Product.INVESTMENTS,
        Product.LIABILITIES,
    ]


if __name__ == "__main__":
    pytest_bazel.main()
