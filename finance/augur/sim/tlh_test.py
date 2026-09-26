from copy import deepcopy
from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.tlh import (
    ModeledRealizations,
    TlhAssumptions,
    TlhMarketUpdate,
    TlhObservation,
    TlhOpening,
    TlhOpeningCohort,
    TlhPortfolio,
    WithdrawalResult,
)


@pytest.fixture
def assumptions() -> TlhAssumptions:
    # Stipulated 1% monthly loss at zero embedded gain, no drawdown response.
    return TlhAssumptions(
        peak_annual_yield=0.12,
        floor_annual_yield=0,
        maturity_decay_exponent=1,
        drawdown_sensitivity=0,
        short_term_fraction=1,
    )


@pytest.fixture
def portfolio(assumptions: TlhAssumptions) -> TlhPortfolio:
    return TlhPortfolio(
        assumptions,
        TlhOpening(
            month=-1, price=100, cohorts=(TlhOpeningCohort(value=10_000, cost_basis=10_000, purchase_month_index=-24),)
        ),
    )


def test_harvest_then_liquidation_conserves_net_gain(portfolio: TlhPortfolio) -> None:
    loss = portfolio.advance(TlhMarketUpdate(month=0, price=100))
    assert loss == ModeledRealizations(short_term_gain=-100)
    assert portfolio.observe() == TlhObservation(value=10_000, reported_tax_basis=9_900)
    sold = portfolio.liquidate()
    assert sold.cash_received == 10_000
    assert sold.realizations == ModeledRealizations(long_term_gain=100)
    assert portfolio.observe() == TlhObservation(value=0, reported_tax_basis=0)


def test_new_contribution_does_not_inherit_prior_harvest(portfolio: TlhPortfolio) -> None:
    portfolio.advance(TlhMarketUpdate(month=0, price=100))
    portfolio.contribute(10_000)
    old_position = portfolio.withdraw(10_000)
    assert old_position.realizations == ModeledRealizations(long_term_gain=100)
    assert portfolio.observe() == TlhObservation(value=10_000, reported_tax_basis=10_000)
    assert portfolio.liquidate().realizations == ModeledRealizations()


def test_each_contribution_has_its_own_loss_capacity(portfolio: TlhPortfolio) -> None:
    portfolio.advance(TlhMarketUpdate(month=0, price=100))
    portfolio.contribute(10_000)
    # Old cohort: 1% embedded gain -> 99 loss. Fresh cohort: 100 loss.
    assert portfolio.advance(TlhMarketUpdate(month=1, price=100)).short_term_gain == -199


def test_partial_withdrawal_preserves_remaining_basis(portfolio: TlhPortfolio) -> None:
    portfolio.advance(TlhMarketUpdate(month=0, price=100))
    first = portfolio.withdraw(3_300)
    assert first.cash_received == 3_300
    assert first.realizations.long_term_gain == 33
    assert portfolio.observe() == TlhObservation(value=6_700, reported_tax_basis=6_633)
    final = portfolio.liquidate()
    assert final.cash_received == 6_700
    assert final.realizations.long_term_gain == 67


def test_a_withdrawal_below_one_unit_sells_exactly_its_value(portfolio: TlhPortfolio) -> None:
    before = portfolio.observe()
    sold = portfolio.withdraw(1)
    assert sold.cash_received == 1
    assert sold.realizations == ModeledRealizations()
    assert portfolio.observe() == TlhObservation(
        value=before.value - 1, reported_tax_basis=before.reported_tax_basis - 1
    )
    assert portfolio.liquidate().cash_received == 9_999


def test_a_contribution_worth_less_than_one_unit_is_exposure_like_any_other(assumptions: TlhAssumptions) -> None:
    portfolio = TlhPortfolio(assumptions, TlhOpening(month=-1, price=100, cohorts=()))
    portfolio.contribute(99)
    # 0.99 of the index halves to 49.5, marked at 50; 1% of that harvests 0.5, rounded to 1.
    assert portfolio.advance(TlhMarketUpdate(month=0, price=50)) == ModeledRealizations(short_term_gain=-1)
    assert portfolio.observe() == TlhObservation(value=50, reported_tax_basis=98)
    # A rate of 2 quanta per unit of the index pays 2 / 50 × 49.5.
    assert portfolio.distribution(2 * MONEY_FACTOR_SCALE) == 2
    assert portfolio.liquidate() == WithdrawalResult(50, ModeledRealizations(short_term_gain=-48))


def test_a_worthless_index_takes_no_contribution_and_opens_only_worthless_cohorts(assumptions: TlhAssumptions) -> None:
    written_off = TlhOpeningCohort(value=0, cost_basis=5, purchase_month_index=-24)
    portfolio = TlhPortfolio(assumptions, TlhOpening(month=-1, price=0, cohorts=(written_off,)))
    assert portfolio.observe() == TlhObservation(value=0, reported_tax_basis=5)
    with pytest.raises(ValueError, match="worthless"):
        portfolio.contribute(1)
    with pytest.raises(ValueError, match="zero opening mark"):
        TlhPortfolio(assumptions, TlhOpening(month=-1, price=0, cohorts=(replace(written_off, value=1),)))


def test_imported_adjusted_basis_is_not_reconstructed(assumptions: TlhAssumptions) -> None:
    portfolio = TlhPortfolio(
        assumptions,
        TlhOpening(
            month=0, price=100, cohorts=(TlhOpeningCohort(value=250, cost_basis=151, purchase_month_index=-24),)
        ),
    )
    assert portfolio.observe() == TlhObservation(value=250, reported_tax_basis=151)
    assert portfolio.liquidate().realizations.long_term_gain == 99


def test_split_withdrawals_deliver_the_value_and_gain_of_one_liquidation(assumptions: TlhAssumptions) -> None:
    # At a price of 3 the opening 10 is 10/3 units of the index: each withdrawal sells a fraction of one.
    portfolio = TlhPortfolio(
        assumptions,
        TlhOpening(month=0, price=3, cohorts=(TlhOpeningCohort(value=10, cost_basis=7, purchase_month_index=-24),)),
    )
    sales = [portfolio.withdraw(3) for _ in range(3)]
    sales.append(portfolio.liquidate())
    assert [sale.cash_received for sale in sales] == [3, 3, 3, 1]
    assert sum(sale.realizations.long_term_gain for sale in sales) == 10 - 7
    assert portfolio.observe() == TlhObservation(value=0, reported_tax_basis=0)


def test_zero_mark_liquidation_releases_all_remaining_basis(portfolio: TlhPortfolio) -> None:
    assert portfolio.advance(TlhMarketUpdate(month=0, price=0)) == ModeledRealizations()
    assert portfolio.withdraw(0).realizations == ModeledRealizations()
    assert portfolio.observe().reported_tax_basis == 10_000
    sold = portfolio.liquidate()
    assert sold.cash_received == 0
    assert sold.realizations.long_term_gain == -10_000
    assert portfolio.observe() == TlhObservation(value=0, reported_tax_basis=0)


def test_rejected_withdrawal_and_bad_month_leave_state_unchanged(portfolio: TlhPortfolio) -> None:
    before = portfolio.observe()
    with pytest.raises(ValueError, match="exceeds"):
        portfolio.withdraw(10_001)
    with pytest.raises(ValueError, match="one month"):
        portfolio.advance(TlhMarketUpdate(month=2, price=200))
    assert portfolio.observe() == before
    assert portfolio.advance(TlhMarketUpdate(month=0, price=100)).short_term_gain == -100


def test_candidate_state_is_isolated_until_settlement(portfolio: TlhPortfolio) -> None:
    candidate = deepcopy(portfolio)
    candidate.advance(TlhMarketUpdate(month=0, price=100))
    candidate.withdraw(500)
    assert portfolio.observe() == TlhObservation(value=10_000, reported_tax_basis=10_000)
    assert candidate.observe() == TlhObservation(value=9_500, reported_tax_basis=9_405)


def test_losses_cannot_reduce_basis_below_zero(assumptions: TlhAssumptions) -> None:
    assumptions = assumptions.model_copy(update={"peak_annual_yield": 120, "floor_annual_yield": 120})
    portfolio = TlhPortfolio(
        assumptions,
        TlhOpening(
            month=-1, price=100, cohorts=(TlhOpeningCohort(value=1_000, cost_basis=3, purchase_month_index=-24),)
        ),
    )
    assert portfolio.advance(TlhMarketUpdate(month=0, price=100)).short_term_gain == -3
    assert portfolio.observe().reported_tax_basis == 0
    assert portfolio.advance(TlhMarketUpdate(month=1, price=100)) == ModeledRealizations()


def test_distribution_uses_component_exposure(portfolio: TlhPortfolio) -> None:
    portfolio.advance(TlhMarketUpdate(month=0, price=100))
    sold = portfolio.withdraw(5_000)
    assert sold.cash_received == 5_000
    assert sold.realizations.long_term_gain == 50
    # 0.1 quanta per unit of the index on the 50 units the remaining 5,000 buys.
    assert portfolio.distribution(MONEY_FACTOR_SCALE // 10) == 5


def test_curve_has_maturity_decay_and_drawdown_response(assumptions: TlhAssumptions) -> None:
    assumptions = assumptions.model_copy(update={"drawdown_sensitivity": 4})
    scale = MONEY_FACTOR_SCALE
    assert assumptions.monthly_loss_fraction(embedded_gain_ppb=0, drawdown_ppb=0) == scale // 100
    assert assumptions.monthly_loss_fraction(embedded_gain_ppb=scale // 2, drawdown_ppb=0) == scale // 200
    assert assumptions.monthly_loss_fraction(embedded_gain_ppb=0, drawdown_ppb=scale // 4) == scale // 50


def test_unrepresentable_rate_is_rejected_before_model_runs() -> None:
    with pytest.raises(ValueError, match="64-bit"):
        TlhAssumptions(
            peak_annual_yield=1e10,
            floor_annual_yield=0,
            maturity_decay_exponent=1,
            drawdown_sensitivity=0,
            short_term_fraction=1,
        )


def test_financial_effects_balance_each_transition(assumptions: TlhAssumptions) -> None:
    portfolio = TlhPortfolio(
        assumptions,
        TlhOpening(
            month=-1, price=77, cohorts=(TlhOpeningCohort(value=3_889, cost_basis=3_999, purchase_month_index=-24),)
        ),
    )
    for month, price in enumerate((77, 43, 100, 99, 120, 3, 0, 50)):
        before = portfolio.observe().reported_tax_basis
        harvest = portfolio.advance(TlhMarketUpdate(month=month, price=price))
        assert portfolio.observe().reported_tax_basis - before == harvest.short_term_gain + harvest.long_term_gain
        before = portfolio.observe().reported_tax_basis
        if price:
            portfolio.contribute(101)
            assert portfolio.observe().reported_tax_basis - before == 101
        before = portfolio.observe().reported_tax_basis
        redemption = portfolio.withdraw(min(113, portfolio.observe().value))
        assert (
            redemption.cash_received + portfolio.observe().reported_tax_basis - before
            == redemption.realizations.short_term_gain + redemption.realizations.long_term_gain
        )
    before = portfolio.observe().reported_tax_basis
    final = portfolio.liquidate()
    assert final.cash_received - before == final.realizations.short_term_gain + final.realizations.long_term_gain
    assert portfolio.observe() == TlhObservation(value=0, reported_tax_basis=0)


if __name__ == "__main__":
    pytest_bazel.main()
