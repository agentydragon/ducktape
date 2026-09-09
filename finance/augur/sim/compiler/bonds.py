"""Income sources contributed by directly held bonds."""

from finance.augur.sim.scenario import InterestIncome, Scenario


def bond_income_categories(scenario: Scenario) -> set[InterestIncome]:
    """Interest sources needed by tax compilation, including issuer jurisdiction."""

    return {InterestIncome(issuer_jurisdiction_id=bond.issuer_jurisdiction_id) for bond in scenario.initial_bonds}
