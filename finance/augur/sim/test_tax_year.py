"""A copied tax book is a detached candidate a settlement path swaps in."""

import pytest_bazel

from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.testing.accounting import HOUSEHOLD, accounting


def test_a_copied_book_shares_no_year_or_income_row_with_the_original() -> None:
    book = accounting().tax
    book.income.accrue(HOUSEHOLD, ORDINARY_INCOME, 100)
    clone = book.copy()
    clone.income.accrue(HOUSEHOLD, ORDINARY_INCOME, 50)
    clone.years[HOUSEHOLD].property_tax_paid = 7
    assert (clone.income.ordinary(HOUSEHOLD), clone.years[HOUSEHOLD].property_tax_paid) == (150, 7)
    assert (book.income.ordinary(HOUSEHOLD), book.years[HOUSEHOLD].property_tax_paid) == (100, 0)


if __name__ == "__main__":
    pytest_bazel.main()
