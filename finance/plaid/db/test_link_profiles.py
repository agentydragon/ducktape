import pytest
import pytest_bazel

from finance.plaid.db.link_profiles import LinkProfile, Product, products_for_profile, profile_catalog


def test_profiles_map_to_minimal_plaid_products() -> None:
    assert products_for_profile(LinkProfile.CASHFLOW) == ["transactions"]
    assert products_for_profile(LinkProfile.CREDIT_CARD_DETAIL) == ["transactions", "liabilities"]
    assert products_for_profile(LinkProfile.INVESTMENTS) == ["investments"]
    assert products_for_profile(LinkProfile.FULL_PICTURE) == ["transactions", "investments", "liabilities"]


def test_advanced_profile_requires_explicit_products() -> None:
    with pytest.raises(ValueError, match="advanced profile requires"):
        products_for_profile(LinkProfile.ADVANCED)
    assert products_for_profile(LinkProfile.ADVANCED, ["liabilities", "transactions", "transactions"]) == [
        "liabilities",
        "transactions",
    ]


def test_catalog_covers_every_profile_and_matches_the_product_map() -> None:
    """The catalog is what the link UI renders, so a profile missing from it is a dropdown entry
    that silently disappears, and a products list that disagrees is the drift this replaced."""
    catalog = profile_catalog()
    assert [entry.value for entry in catalog] == list(LinkProfile)
    for entry in catalog:
        profile = entry.value
        assert entry.label, f"{profile} has no label"
        if profile is not LinkProfile.ADVANCED:
            assert entry.products == products_for_profile(profile)


def test_advanced_declares_no_fixed_products() -> None:
    """It cannot: the caller supplies them. The UI keys off the empty list to show the checkboxes."""
    advanced = next(e for e in profile_catalog() if e.value is LinkProfile.ADVANCED)
    assert advanced.products == []


def test_full_picture_is_the_widest_and_therefore_the_most_fragile() -> None:
    """Plaid fails the whole Link when the institution lacks any requested product, so the profile
    whose name promises the most is the one most likely to fail — the reason labels name scopes."""
    catalog = {e.value: e for e in profile_catalog()}
    full = catalog[LinkProfile.FULL_PICTURE].products
    assert set(full) == {Product.TRANSACTIONS, Product.INVESTMENTS, Product.LIABILITIES}
    for value, entry in catalog.items():
        if value is not LinkProfile.FULL_PICTURE:
            assert set(entry.products) <= set(full)


if __name__ == "__main__":
    pytest_bazel.main()
