"""Income sources contributed by directly held bonds."""

from collections.abc import Iterable

from finance.augur.sim.scenario import BondHolding, InterestIncome


def bond_income_categories(bonds: Iterable[BondHolding]) -> set[InterestIncome]:
    """Interest sources needed by tax compilation, including issuer jurisdiction."""

    return {InterestIncome(issuer_jurisdiction_id=bond.issuer_jurisdiction_id) for bond in bonds}
