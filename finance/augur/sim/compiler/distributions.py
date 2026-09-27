"""Income sources contributed by public-security distributions."""

from collections.abc import Iterable

from finance.augur.sim.scenario import SecurityDistribution, TransferIncomeCategory


def distribution_income_categories(distributions: Iterable[SecurityDistribution]) -> set[TransferIncomeCategory]:
    """Income sources needed by tax compilation for every distribution slice."""

    return {tax_slice.income_category for distribution in distributions for tax_slice in distribution.tax_character}
