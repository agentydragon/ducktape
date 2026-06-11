import pytest
import pytest_bazel

from finance.plaid.db.link_profiles import LinkProfile, products_for_profile, syncs_investment_transactions


def test_profiles_map_to_minimal_plaid_products() -> None:
    assert products_for_profile(LinkProfile.CASHFLOW) == ["transactions"]
    assert products_for_profile(LinkProfile.CREDIT_CARD_DETAIL) == ["transactions", "liabilities"]
    assert products_for_profile(LinkProfile.INVESTMENTS_HOLDINGS) == ["investments"]
    assert products_for_profile(LinkProfile.INVESTMENTS_FULL) == ["investments"]
    assert products_for_profile(LinkProfile.FULL_PICTURE) == ["transactions", "investments", "liabilities"]


def test_advanced_profile_requires_explicit_products() -> None:
    with pytest.raises(ValueError, match="advanced profile requires"):
        products_for_profile(LinkProfile.ADVANCED)
    assert products_for_profile(LinkProfile.ADVANCED, ["liabilities", "transactions", "transactions"]) == [
        "liabilities",
        "transactions",
    ]


def test_investment_transaction_sync_is_profile_gated() -> None:
    assert not syncs_investment_transactions(LinkProfile.INVESTMENTS_HOLDINGS)
    assert syncs_investment_transactions(LinkProfile.INVESTMENTS_FULL)
    assert syncs_investment_transactions(LinkProfile.FULL_PICTURE)


if __name__ == "__main__":
    pytest_bazel.main()
