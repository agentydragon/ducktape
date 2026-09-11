"""Year close integrates netting, deductions, SALT, liabilities and carryover reset."""

from copy import deepcopy
from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.ledger import Ledger
from finance.augur.sim.prepared import _SaltCap, _SaltDeduction
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, flat_rules, prepared_scenario


def test_year_close_nets_once_then_reassesses_federal_salt_and_resets() -> None:
    scenario = prepared_scenario()
    profile = replace(
        scenario.tax_profiles[0],
        jurisdictions=tuple(
            replace(flat_rules(name, rate), max_capital_loss_ordinary_offset=300)
            for name, rate in [("test_federal", 100_000_000), ("test_state", 200_000_000)]
        ),
    )
    scenario = replace(
        scenario,
        tax_profiles=(profile,),
        _federal_salt_deduction_policies=(
            _SaltDeduction(
                profile_id=HOUSEHOLD,
                federal_jurisdiction_id="test_federal",
                cap_schedule=(_SaltCap(effective_year_index=0, cap=1000),),
            ),
        ),
    )
    books = Accounting(scenario.accounts, scenario.tax_profiles, scenario.income_sources, capture="forensic")
    books.tax.income.accrue(HOUSEHOLD, ORDINARY_INCOME, 10_000)
    books.tax.gain(HOUSEHOLD, -700, long_term=False)
    year = books.tax.years[HOUSEHOLD]
    year.rental_interest_deduction = 60
    year.depreciation_deduction = 40
    before = deepcopy(books.tax.years)
    quoted = books.tax.assessments(scenario, 11, [])
    assert books.tax.years == before
    federal, state = quoted
    assert federal.ordinary_income == state.ordinary_income == 9600
    assert (state.total_tax, federal.total_tax, federal.salt_deduction) == (1920, 860, 1000)
    assert federal.capital_loss_carryforward == state.capital_loss_carryforward == 400
    books.close_tax_year(scenario, 11, [])
    assert books.tax_accruals == quoted
    assert books.ledger.balance(CASH) == 100
    assert books.ledger.trial_balance() == 0
    assert [liability.amount_owed for liability in books.tax_liabilities] == [860, 1920]
    assert books.tax.income.ordinary(HOUSEHOLD) == 0
    assert books.tax.years[HOUSEHOLD].capital_loss_carryforward == 400
    assert books.tax.years[HOUSEHOLD].short_term_gain == 0
    assert books.tax.years[HOUSEHOLD].depreciation_deduction == 0
    following = books.tax.assessments(scenario, 23, [])
    assert all(row.capital_loss_carryforward == 100 for row in following)
    assert all(row.total_tax == 0 for row in following)


def test_year_close_rejection_keeps_income_carryovers_and_all_jurisdictions_uncommitted() -> None:
    scenario = prepared_scenario()
    books = Accounting(scenario.accounts, scenario.tax_profiles, scenario.income_sources, capture="forensic")
    books.tax.income.accrue(HOUSEHOLD, ORDINARY_INCOME, 1000)
    books.ledger = Ledger(
        account for account in books.ledger.balances if account.account_id != "liability:tax:test_federal"
    )
    before = deepcopy(books.tax)
    journal = list(books.journal)
    balances = dict(books.ledger.balances)
    with pytest.raises(KeyError):
        books.close_tax_year(scenario, 11, [])
    assert books.tax.income.by_source == before.income.by_source
    assert books.tax.years == before.years
    assert books.journal == journal
    assert dict(books.ledger.balances) == balances
    assert not books.tax_accruals
    assert not books.tax_liabilities


if __name__ == "__main__":
    pytest_bazel.main()
