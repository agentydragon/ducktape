"""Tests for the constant-maturity bond fund.

The load-bearing one is `test_the_annual_step_reproduces_the_reference_dataset`: this
construction is only worth having if it is the SAME one the published sources use, and that is
checkable rather than assertable.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.bond_fund import MONTHS_PER_YEAR, constant_maturity_fund_paths, par_bond_price

# Damodaran's historical-returns dataset (NYU Stern, `histretSP.xlsx`), sheet "Returns by year"
# column F, against the Moody's Baa yields on its own "Moody's Rates" sheet — themselves FRED
# `BAA`. Each row is (previous year's yield, this year's yield, his published annual return) at
# the 10-year maturity his sheet documents. An external contract, so these are literals.
DAMODARAN_BAA_YEARS = (
    (0.0532, 0.0560, 0.032195514702324381),
    (0.0560, 0.0595, 0.030178562399040432),
    (0.0595, 0.0671, 0.0053978094648238287),
    (0.0671, 0.1042, -0.15680775082667592),
    (0.1042, 0.0842, 0.23589601675740196),
    (0.0842, 0.0775, 0.1296689369754826),
)


def test_the_annual_step_reproduces_the_reference_dataset() -> None:
    """One period of this construction IS the reference's annual step, to the last bit.

    Including 1931, where the Baa yield went 6.71% -> 10.42% and the bond lost 15.7%: a
    duration approximation misses that by over a point, which is the size of error that decides
    a withdrawal study.
    """

    for previous_yield, current_yield, published in DAMODARAN_BAA_YEARS:
        price = par_bond_price(np.array([previous_yield]), np.array([current_yield]), maturity_years=10)
        annual_return = float(price[0] - 1.0) + previous_yield
        assert annual_return == pytest.approx(published, abs=1e-12)


def test_a_bond_priced_at_its_own_coupon_is_worth_par() -> None:
    """The definition the whole construction rests on: the fund buys at par every period, so a
    flat yield path must leave the mark exactly unmoved rather than nearly so."""

    for rate in (0.001, 0.03, 0.0775, 0.25):
        assert float(par_bond_price(np.array([rate]), np.array([rate]), maturity_years=10)[0]) == pytest.approx(1.0)


def test_a_flat_yield_path_pays_its_coupon_and_never_moves_the_mark() -> None:
    yields = np.full((1, 25), 0.06)
    price, distribution = constant_maturity_fund_paths(yields, maturity_years=10, initial_price_usd=100.0)

    assert np.allclose(price, 100.0)
    assert np.allclose(distribution, 100.0 * 0.06 / MONTHS_PER_YEAR)


def test_the_payout_yield_on_the_mark_tracks_the_yield_of_the_bonds_held() -> None:
    """The invariant the old duration model broke: a fund cannot pay a yield on its own net
    assets that its holdings do not earn. Pin it where it used to fail worst — a long stretch
    of rising yields, which is what drives the mark away from where it started."""

    rising = np.linspace(0.03, 0.10, 400)[None, :]
    price, distribution = constant_maturity_fund_paths(rising, maturity_years=10, initial_price_usd=100.0)

    # Distribution is struck on the PREVIOUS mark against the previous yield, so the ratio is
    # that yield exactly -- no drift term, at any distance from the start.
    yield_on_mark = MONTHS_PER_YEAR * distribution[0, 1:] / price[0, :-1]
    assert np.allclose(yield_on_mark, rising[0, :-1])
    # And at the boundary, where the position opens at its own month's yield.
    assert MONTHS_PER_YEAR * distribution[0, 0] / 100.0 == pytest.approx(rising[0, 0])


def test_rising_yields_mark_the_fund_down_and_falling_yields_mark_it_up() -> None:
    for path, expected_lower in ((np.linspace(0.03, 0.09, 120), True), (np.linspace(0.09, 0.03, 120), False)):
        price, _ = constant_maturity_fund_paths(path[None, :], maturity_years=10, initial_price_usd=100.0)
        assert bool(price[0, -1] < 100.0) is expected_lower


def test_a_longer_maturity_moves_more_for_the_same_yield_change() -> None:
    """Duration is an OUTPUT of this construction rather than a parameter of it, so the
    ordering it used to be handed has to fall out of the bond math instead."""

    jump = np.array([[0.04, 0.05]])
    marks = [
        float(constant_maturity_fund_paths(jump, maturity_years=years, initial_price_usd=100.0)[0][0, -1])
        for years in (2.0, 10.0, 30.0)
    ]
    assert marks[0] > marks[1] > marks[2]


def test_every_rollout_is_priced_independently() -> None:
    yields = np.array([[0.04, 0.05, 0.06], [0.08, 0.07, 0.06]])
    price, distribution = constant_maturity_fund_paths(yields, maturity_years=10, initial_price_usd=100.0)

    for rollout in range(2):
        alone, alone_distribution = constant_maturity_fund_paths(
            yields[rollout : rollout + 1], maturity_years=10, initial_price_usd=100.0
        )
        assert np.allclose(price[rollout], alone[0])
        assert np.allclose(distribution[rollout], alone_distribution[0])


if __name__ == "__main__":
    pytest_bazel.main()
