"""Income categories keep their reporting identity without tensor-row bookkeeping."""

import pytest
import pytest_bazel

from finance.augur.sim.compiler.income_sources import income_source_sort_key, income_source_wire_id
from finance.augur.sim.scenario import InterestIncome, OrdinaryIncome, TransferIncomeCategory


@pytest.mark.parametrize(
    ("source", "wire_id"),
    [
        (OrdinaryIncome(), "ordinary"),
        (InterestIncome(issuer_jurisdiction_id="federal_us"), "interest:federal_us"),
        (InterestIncome(issuer_jurisdiction_id="california"), "interest:california"),
        (InterestIncome(), "interest:corporate"),
    ],
)
def test_income_category_identity(source: TransferIncomeCategory, wire_id: str) -> None:
    assert income_source_wire_id(source) == wire_id


def test_reporting_order_places_corporate_interest_after_named_issuers() -> None:
    sources: list[TransferIncomeCategory] = [
        InterestIncome(),
        InterestIncome(issuer_jurisdiction_id="federal_us"),
        OrdinaryIncome(),
        InterestIncome(issuer_jurisdiction_id="california"),
    ]
    assert [income_source_wire_id(source) for source in sorted(sources, key=income_source_sort_key)] == [
        "ordinary",
        "interest:california",
        "interest:federal_us",
        "interest:corporate",
    ]


if __name__ == "__main__":
    pytest_bazel.main()
