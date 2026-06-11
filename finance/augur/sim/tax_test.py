"""Tests for the marginal-rate bracket walk."""

from __future__ import annotations

import math

import numpy as np
import pytest_bazel

from finance.augur.sim.jurisdictions import TaxBracket, load_jurisdiction
from finance.augur.sim.tax import apply_brackets, apply_ltcg_brackets, net_capital_gains_with_carryforward


def test_apply_brackets_hand_computed_federal_single_50k() -> None:
    """Federal single filer on $50k taxable income (2024 brackets):
    10% × 11600 + 12% × (47150-11600) + 22% × (50000-47150)
    = 1160.00 + 4266.00 + 627.00 = 6053.00."""
    fed = load_jurisdiction("federal_us")
    tax = apply_brackets(np.array([50_000.0]), fed.ordinary_income_brackets["single"])
    assert math.isclose(tax[0], 6053.0, abs_tol=1e-6)


def test_apply_brackets_hand_computed_federal_single_200k() -> None:
    """Federal single filer on $200k taxable income:
    10% × 11600 + 12% × 35550 + 22% × 53375 + 24% × 91425 + 32% × 8050
    = 1160 + 4266 + 11742.50 + 21942.00 + 2576.00 = 41686.50."""
    fed = load_jurisdiction("federal_us")
    tax = apply_brackets(np.array([200_000.0]), fed.ordinary_income_brackets["single"])
    assert math.isclose(tax[0], 41_686.5, abs_tol=1e-6)


def test_apply_brackets_vectorized_handles_multiple_rollouts() -> None:
    """Walk a vector of incomes in one pass — every rollout
    consumes the same schedule but produces independent tax
    figures."""
    fed = load_jurisdiction("federal_us")
    incomes = np.array([0.0, 11_600.0, 50_000.0, 200_000.0])
    tax = apply_brackets(incomes, fed.ordinary_income_brackets["single"])
    assert math.isclose(tax[0], 0.0)
    assert math.isclose(tax[1], 1160.0, abs_tol=1e-6)
    assert math.isclose(tax[2], 6053.0, abs_tol=1e-6)
    assert math.isclose(tax[3], 41_686.5, abs_tol=1e-6)


def test_apply_brackets_negative_input_zeroes_out() -> None:
    """Sub-zero amounts (e.g. after subtracting the standard
    deduction from a low income) produce zero tax."""
    brackets = [TaxBracket(upper_usd=10.0, rate=0.1), TaxBracket(upper_usd=math.inf, rate=0.2)]
    tax = apply_brackets(np.array([-50.0, 0.0, 5.0, 15.0]), brackets)
    assert tax.tolist() == [0.0, 0.0, 0.5, 2.0]


def test_apply_brackets_california_single_100k() -> None:
    """California single filer on $100k:
    1% × 10412 + 2% × 14272 + 4% × 14275 + 6% × 15122 + 8% × 14269 + 9.3% × 31650
    = 104.12 + 285.44 + 571.00 + 907.32 + 1141.52 + 2943.45 = 5952.85."""
    ca = load_jurisdiction("california")
    tax = apply_brackets(np.array([100_000.0]), ca.ordinary_income_brackets["single"])
    assert math.isclose(tax[0], 5952.85, abs_tol=0.01)


def test_ltcg_brackets_zero_when_total_taxable_below_threshold() -> None:
    """Ordinary $30k + LTCG $10k = $40k total. The federal 0% LTCG
    bracket extends to $47,025 so all $10k of LTCG falls in it →
    zero tax."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    tax = apply_ltcg_brackets(np.array([10_000.0]), np.array([30_000.0]), fed.ltcg_brackets["single"])
    assert math.isclose(tax[0], 0.0, abs_tol=1e-6)


def test_ltcg_brackets_split_across_zero_and_fifteen_percent() -> None:
    """Ordinary $30k + LTCG $20k = $50k total. The 0% bracket ends
    at $47,025; LTCG occupies $30k-$50k.
    0% rate on $30k-$47,025 = $17,025 → $0.
    15% rate on $47,025-$50,000 = $2,975 → $446.25.
    Total LTCG tax = $446.25."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    tax = apply_ltcg_brackets(np.array([20_000.0]), np.array([30_000.0]), fed.ltcg_brackets["single"])
    assert math.isclose(tax[0], 446.25, abs_tol=1e-6)


def test_ltcg_brackets_pure_fifteen_when_ordinary_already_in_15pct_band() -> None:
    """Ordinary income $100k already past the 0% LTCG threshold.
    LTCG $50k sits entirely in the 15% bracket → $7500."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    tax = apply_ltcg_brackets(np.array([50_000.0]), np.array([100_000.0]), fed.ltcg_brackets["single"])
    assert math.isclose(tax[0], 7500.0, abs_tol=1e-6)


def test_ltcg_brackets_vectorized() -> None:
    """One call, four rollouts, each with its own ordinary + LTCG
    combination — all bracket walks share the same schedule."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    tax = apply_ltcg_brackets(
        np.array([10_000.0, 20_000.0, 50_000.0, 0.0]),
        np.array([30_000.0, 30_000.0, 100_000.0, 0.0]),
        fed.ltcg_brackets["single"],
    )
    assert math.isclose(tax[0], 0.0, abs_tol=1e-6)
    assert math.isclose(tax[1], 446.25, abs_tol=1e-6)
    assert math.isclose(tax[2], 7500.0, abs_tol=1e-6)
    assert math.isclose(tax[3], 0.0, abs_tol=1e-6)


def test_net_capital_gains_pure_gains_pass_through() -> None:
    """With no losses and no carryforward, ST/LT gains are returned untouched and nothing offsets
    ordinary income — the netting must be a no-op for the common all-gains year."""
    net_st, net_lt, offset, carry_out = net_capital_gains_with_carryforward(
        np.array([4_000.0]), np.array([10_000.0]), np.array([0.0])
    )
    assert math.isclose(net_st[0], 4_000.0)
    assert math.isclose(net_lt[0], 10_000.0)
    assert math.isclose(offset[0], 0.0)
    assert math.isclose(carry_out[0], 0.0)


def test_net_capital_gains_short_term_loss_offsets_long_term_gain() -> None:
    """A net short-term loss cross-nets against a long-term gain before either is taxed."""
    net_st, net_lt, offset, carry_out = net_capital_gains_with_carryforward(
        np.array([-3_000.0]), np.array([10_000.0]), np.array([0.0])
    )
    assert math.isclose(net_st[0], 0.0)
    assert math.isclose(net_lt[0], 7_000.0)
    assert math.isclose(offset[0], 0.0)
    assert math.isclose(carry_out[0], 0.0)


def test_net_capital_gains_net_loss_caps_ordinary_offset_and_carries_remainder() -> None:
    """A $12k net capital loss deducts $3k against ordinary income this year and carries the
    remaining $9k forward; no gains remain to tax."""
    net_st, net_lt, offset, carry_out = net_capital_gains_with_carryforward(
        np.array([-5_000.0]), np.array([-7_000.0]), np.array([0.0])
    )
    assert math.isclose(net_st[0], 0.0)
    assert math.isclose(net_lt[0], 0.0)
    assert math.isclose(offset[0], 3_000.0)
    assert math.isclose(carry_out[0], 9_000.0)


def test_net_capital_gains_small_loss_fully_offsets_ordinary() -> None:
    """A net loss below the $3k cap is fully deducted with nothing carried forward."""
    _, _, offset, carry_out = net_capital_gains_with_carryforward(
        np.array([-1_200.0]), np.array([0.0]), np.array([0.0])
    )
    assert math.isclose(offset[0], 1_200.0)
    assert math.isclose(carry_out[0], 0.0)


def test_net_capital_gains_carryforward_consumes_following_year_gain() -> None:
    """A prior-year carryforward offsets this year's gains (short-term first), shrinking the taxed
    gain and the carryforward balance."""
    net_st, net_lt, offset, carry_out = net_capital_gains_with_carryforward(
        np.array([2_000.0]), np.array([5_000.0]), np.array([9_000.0])
    )
    # $9k carryforward wipes the $2k ST gain and $5k LT gain (total $7k); the unused $2k is then a
    # net capital loss that deducts (under the $3k cap) against ordinary income, leaving $0 to carry.
    assert math.isclose(net_st[0], 0.0)
    assert math.isclose(net_lt[0], 0.0)
    assert math.isclose(offset[0], 2_000.0)
    assert math.isclose(carry_out[0], 0.0)


def test_net_capital_gains_vectorized_independent_rollouts() -> None:
    """One call over multiple rollouts: a gain year, a carryforward-consumed year, and a
    net-loss year each resolve independently."""
    net_st, net_lt, offset, carry_out = net_capital_gains_with_carryforward(
        np.array([4_000.0, 1_000.0, -2_000.0]), np.array([10_000.0, 1_000.0, -6_000.0]), np.array([0.0, 0.0, 0.0])
    )
    assert np.allclose(net_st, [4_000.0, 1_000.0, 0.0])
    assert np.allclose(net_lt, [10_000.0, 1_000.0, 0.0])
    assert np.allclose(offset, [0.0, 0.0, 3_000.0])
    assert np.allclose(carry_out, [0.0, 0.0, 5_000.0])


if __name__ == "__main__":
    pytest_bazel.main()
