"""Income categories sort for reporting and keep a stable wire id."""

import pytest_bazel

from finance.augur.sim.ids import JurisdictionId
from finance.augur.sim.income import (
    InterestIncome,
    Municipal,
    OrdinaryIncome,
    Taxable,
    TransferIncomeCategory,
    Treasury,
    income_source_sort_key,
    income_source_wire_id,
)


def test_reporting_order_places_taxable_interest_last() -> None:
    sources: list[TransferIncomeCategory] = [
        InterestIncome(character=Taxable()),
        InterestIncome(character=Treasury()),
        OrdinaryIncome(),
        InterestIncome(character=Municipal(state=JurisdictionId("test_state"))),
    ]
    assert [income_source_wire_id(source) for source in sorted(sources, key=income_source_sort_key)] == [
        "ordinary",
        "interest:municipal:test_state",
        "interest:treasury",
        "interest:taxable",
    ]


if __name__ == "__main__":
    pytest_bazel.main()
