"""Income sources contributed by public-security distributions."""

from finance.augur.sim.scenario import InterestIncome, Scenario


def distribution_income_categories(scenario: Scenario) -> set[InterestIncome]:
    """Interest sources needed by tax compilation for every distribution slice."""

    return {
        InterestIncome(issuer_jurisdiction_id=tax_slice.issuer_jurisdiction_id)
        for distribution in scenario.security_distributions
        for tax_slice in distribution.tax_character
    }
