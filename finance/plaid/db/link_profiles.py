"""Human-facing Plaid link profiles.

The UI presents financial data intents rather than raw Plaid product names. These
profiles are persisted on the link row so future readers can understand why an
Item has a particular product set.
"""

from enum import StrEnum


class Product(StrEnum):
    TRANSACTIONS = "transactions"
    INVESTMENTS = "investments"
    LIABILITIES = "liabilities"


class LinkProfile(StrEnum):
    CASHFLOW = "cashflow"
    CREDIT_CARD_DETAIL = "credit_card_detail"
    INVESTMENTS_HOLDINGS = "investments_holdings"
    INVESTMENTS_FULL = "investments_full"
    FULL_PICTURE = "full_picture"
    ADVANCED = "advanced"


PROFILE_PRODUCTS: dict[LinkProfile, tuple[Product, ...]] = {
    LinkProfile.CASHFLOW: (Product.TRANSACTIONS,),
    LinkProfile.CREDIT_CARD_DETAIL: (Product.TRANSACTIONS, Product.LIABILITIES),
    LinkProfile.INVESTMENTS_HOLDINGS: (Product.INVESTMENTS,),
    LinkProfile.INVESTMENTS_FULL: (Product.INVESTMENTS,),
    LinkProfile.FULL_PICTURE: (Product.TRANSACTIONS, Product.INVESTMENTS, Product.LIABILITIES),
}


def products_for_profile(profile: LinkProfile, advanced_products: list[str] | None = None) -> list[str]:
    """Return Plaid product names for a UI profile."""
    if profile is LinkProfile.ADVANCED:
        if not advanced_products:
            raise ValueError("advanced profile requires at least one product")
        return sorted(set(advanced_products))
    return [p.value for p in PROFILE_PRODUCTS[profile]]


def syncs_investment_transactions(profile: LinkProfile) -> bool:
    """Whether v0 should call /investments/transactions/get for this profile."""
    return profile in {LinkProfile.INVESTMENTS_FULL, LinkProfile.FULL_PICTURE}
