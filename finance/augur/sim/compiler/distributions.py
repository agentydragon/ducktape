"""Income sources contributed by public-security distributions."""

from collections.abc import Iterable

from finance.augur.sim.scenario import InterestIncome, SecurityDistribution


def distribution_income_categories(distributions: Iterable[SecurityDistribution]) -> set[InterestIncome]:
    """Interest sources needed by tax compilation for every distribution slice."""

    return {
        InterestIncome(issuer_jurisdiction_id=tax_slice.issuer_jurisdiction_id)
        for distribution in distributions
        for tax_slice in distribution.tax_character
    }
