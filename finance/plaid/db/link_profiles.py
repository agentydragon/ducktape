"""Human-facing Plaid link profiles.

The UI presents financial data intents rather than raw Plaid product names. These
profiles are persisted on the link row so future readers can understand why an
Item has a particular product set.
"""

from enum import StrEnum

from pydantic import BaseModel


class Product(StrEnum):
    TRANSACTIONS = "transactions"
    INVESTMENTS = "investments"
    LIABILITIES = "liabilities"


class LinkProfile(StrEnum):
    CASHFLOW = "cashflow"
    CREDIT_CARD_DETAIL = "credit_card_detail"
    INVESTMENTS = "investments"
    FULL_PICTURE = "full_picture"
    ADVANCED = "advanced"


PROFILE_PRODUCTS: dict[LinkProfile, tuple[Product, ...]] = {
    LinkProfile.CASHFLOW: (Product.TRANSACTIONS,),
    LinkProfile.CREDIT_CARD_DETAIL: (Product.TRANSACTIONS, Product.LIABILITIES),
    LinkProfile.INVESTMENTS: (Product.INVESTMENTS,),
    LinkProfile.FULL_PICTURE: (Product.TRANSACTIONS, Product.INVESTMENTS, Product.LIABILITIES),
}


# What each profile is called in the UI. Kept next to the product map so a new profile cannot be
# added without deciding how it presents — the two used to live in hand-written JS in the link app,
# free to drift from the products actually requested.
PROFILE_LABELS: dict[LinkProfile, str] = {
    LinkProfile.CASHFLOW: "Cashflow",
    LinkProfile.CREDIT_CARD_DETAIL: "Credit card detail",
    LinkProfile.INVESTMENTS: "Investments",
    LinkProfile.FULL_PICTURE: "Full picture",
    LinkProfile.ADVANCED: "Advanced",
}


class ProfileInfo(BaseModel):
    """One data-surface choice as the link UI renders it. Lives here rather than in the web app so
    the dropdown cannot promise a surface the backend does not request."""

    value: LinkProfile
    label: str
    products: list[Product]


def profile_catalog() -> list[ProfileInfo]:
    """Every profile as the UI needs it.

    The products belong in the label because Plaid fails the *whole* Link when the institution does
    not support every requested product — so a profile named for an intent ("Full picture") reads as
    a richer choice while actually being the most fragile one. Naming the scopes makes the trade
    visible at the point of choosing. `advanced` has no fixed products; the caller supplies them.
    """
    return [
        ProfileInfo(value=profile, label=PROFILE_LABELS[profile], products=list(PROFILE_PRODUCTS.get(profile, ())))
        for profile in LinkProfile
    ]


def products_for_profile(profile: LinkProfile, advanced_products: list[str] | None = None) -> list[str]:
    """Return Plaid product names for a UI profile."""
    if profile is LinkProfile.ADVANCED:
        if not advanced_products:
            raise ValueError("advanced profile requires at least one product")
        return sorted(set(advanced_products))
    return [p.value for p in PROFILE_PRODUCTS[profile]]
