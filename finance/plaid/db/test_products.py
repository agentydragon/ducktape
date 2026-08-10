import pytest_bazel

from finance.plaid.db.products import Product, syncable_products


def test_syncable_products_intersects_and_orders() -> None:
    assert syncable_products(["liabilities", "auth", "transactions"]) == [Product.TRANSACTIONS, Product.LIABILITIES]


def test_products_the_app_cannot_sync_are_dropped() -> None:
    """An institution supporting only auth/identity offers nothing this app mirrors, and Link must
    not be opened requesting a product that would fail it."""
    assert syncable_products(["auth", "identity", "income_verification"]) == []


def test_an_institution_supporting_everything_offers_all_three() -> None:
    assert syncable_products([p.value for p in Product] + ["auth"]) == list(Product)


if __name__ == "__main__":
    pytest_bazel.main()
