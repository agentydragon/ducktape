"""Hand-computed bracket cases for the tax math the engine actually ships.

`_apply_brackets`, `_apply_ltcg_brackets` and `_net_capital_gains_jnp` are pure and
branch-free, so they run eagerly on concrete arrays — the same code the `lax.scan` traces
at year-end. Everything here is int64 currency quanta laid out the way `compile_tax` lays it out
(quantum-count bracket edges with the int64 sentinel for the open-ended top, float rates, an active
prefix `count`), because that is what the engine is handed at runtime.

Deviation from a textbook bracket walk: each walk rounds half-up to a whole currency quantum,
so a schedule whose rate is not representable in binary floating point still lands on an exact
represented amount.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

import jax
import jax.numpy as jnp
import pytest_bazel

from finance.augur.sim.compiler.tax import bracket_upper_to_quanta
from finance.augur.sim.engine.jax_engine import (
    _apply_brackets,
    _apply_ltcg_brackets,
    _net_capital_gains_jnp,
    _scale_money,
    _scale_quanta_by_ratio,
    _sum_money_with_factors,
    _value_quanta_from_quantity,
)
from finance.augur.sim.jurisdictions import TaxBracket, load_jurisdiction


class _BracketTable(TypedDict):
    upper: jnp.ndarray
    rate: jnp.ndarray
    count: int


def _brackets(schedule: list[TaxBracket]) -> _BracketTable:
    return _BracketTable(
        upper=jnp.asarray(
            [bracket_upper_to_quanta(bracket.upper, currency_quantum="0.01") for bracket in schedule], dtype=jnp.int64
        ),
        rate=jnp.asarray([bracket.rate for bracket in schedule], dtype=jnp.float64),
        count=len(schedule),
    )


# The IRC 1211(b) cap these cases net against. Stated here rather than imported from the
# jurisdiction data: what is being checked is the netting arithmetic at a known cap, not that
# the reducer and the YAML hold the same number.
_CAPITAL_LOSS_OFFSET_CAP = jnp.asarray([300_000], dtype=jnp.int64)


def _net(
    short_term: jnp.ndarray, long_term: jnp.ndarray, carryforward: jnp.ndarray
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    return _net_capital_gains_jnp(
        short_term, long_term, carryforward, max_ordinary_offset_quanta=_CAPITAL_LOSS_OFFSET_CAP
    )


def _quanta(*usd: float) -> jnp.ndarray:
    return jnp.asarray(
        [int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for value in usd],
        dtype=jnp.int64,
    )


def test_integer_money_scaling_never_needs_a_float_money_value() -> None:
    assert _scale_money(jnp.asarray([9_007_199_254_740_993], dtype=jnp.int64), 0.5).tolist() == [4_503_599_627_370_497]
    # The direct product overflows int64, while the quotient/remainder result fits.
    amount = jnp.asarray([4_800_000_000_000_000_000], dtype=jnp.int64)
    numerator = jnp.asarray(2, dtype=jnp.int64)
    denominator = jnp.asarray(3, dtype=jnp.int64)
    assert _scale_quanta_by_ratio(amount, numerator, denominator).tolist() == [3_200_000_000_000_000_000]
    assert _value_quanta_from_quantity(amount, numerator, denominator).tolist() == [3_200_000_000_000_000_000]
    # Signed exact halves round away from zero (Decimal ROUND_HALF_UP semantics).
    assert _scale_quanta_by_ratio(
        jnp.asarray([-1, 1], dtype=jnp.int64), jnp.asarray(1, dtype=jnp.int64), jnp.asarray(2, dtype=jnp.int64)
    ).tolist() == [-1, 1]


def test_scaled_money_sum_rounds_the_non_negative_aggregate_once() -> None:
    # Each term is 0.4 quantum. Per-term rounding would lose both; aggregate rounding is 1.
    assert _sum_money_with_factors(
        jnp.asarray([[1], [1]], dtype=jnp.int64), jnp.asarray([[400_000_000], [400_000_000]], dtype=jnp.int64), axis=0
    ).tolist() == [1]


def test_apply_brackets_hand_computed_federal_single_50k() -> None:
    """Federal single filer on $50k taxable income (2024 brackets):
    10% × 11600 + 12% × (47150-11600) + 22% × (50000-47150)
    = 1160.00 + 4266.00 + 627.00 = 6053.00."""
    fed = load_jurisdiction("federal_us")
    tax = _apply_brackets(_quanta(50_000.0), **_brackets(fed.ordinary_income_brackets["single"]))
    assert tax.tolist() == [605_300]


def test_apply_brackets_hand_computed_federal_single_200k() -> None:
    """Federal single filer on $200k taxable income:
    10% × 11600 + 12% × 35550 + 22% × 53375 + 24% × 91425 + 32% × 8050
    = 1160 + 4266 + 11742.50 + 21942.00 + 2576.00 = 41686.50."""
    fed = load_jurisdiction("federal_us")
    tax = _apply_brackets(_quanta(200_000.0), **_brackets(fed.ordinary_income_brackets["single"]))
    assert tax.tolist() == [4_168_650]


def test_apply_brackets_vectorized_handles_multiple_rollouts() -> None:
    """Walk a vector of incomes in one pass — every rollout consumes the same schedule but
    produces independent tax figures."""
    fed = load_jurisdiction("federal_us")
    tax = _apply_brackets(
        _quanta(0.0, 11_600.0, 50_000.0, 200_000.0), **_brackets(fed.ordinary_income_brackets["single"])
    )
    assert tax.tolist() == [0, 116_000, 605_300, 4_168_650]


def test_apply_brackets_negative_input_zeroes_out() -> None:
    """Sub-zero amounts (e.g. after subtracting the standard deduction from a low income)
    produce zero tax."""
    schedule = [TaxBracket(upper=10, rate=0.1), TaxBracket(upper="Infinity", rate=0.2)]
    tax = _apply_brackets(_quanta(-50.0, 0.0, 5.0, 15.0), **_brackets(schedule))
    assert tax.tolist() == [0, 0, 50, 200]


def test_apply_brackets_california_single_100k() -> None:
    """California single filer on $100k:
    1% × 10412 + 2% × 14272 + 4% × 14275 + 6% × 15122 + 8% × 14269 + 9.3% × 31650
    = 104.12 + 285.44 + 571.00 + 907.32 + 1141.52 + 2943.45 = 5952.85.

    Exact where the float64 walk this replaced was not: 9.3% has no binary floating-point
    representation, so that version summed to 5952.849999999999 and its assertion had to
    carry a one-cent tolerance. Rounding to int64 cents lands on 595285 on the nose."""
    ca = load_jurisdiction("california")
    tax = _apply_brackets(_quanta(100_000.0), **_brackets(ca.ordinary_income_brackets["single"]))
    assert tax.tolist() == [595_285]


def test_apply_brackets_rounds_a_split_cent_away_from_zero() -> None:
    """A walk landing on EXACTLY half a cent rounds up, not to even.

    $0.04 at 12.5% is 0.5 cents on the nose, which is the only input that separates the two
    plausible rules: half-away-from-zero gives 1, banker's rounding gives 0. Anything landing
    off the midpoint — 0.625 cents, say — rounds to 1 under every rule and would pin nothing.

    The float64 walk this replaced had no rounding rule at all; it handed back a fraction of
    a dollar and left rounding to whoever spent it.
    """
    tax = _apply_brackets(_quanta(0.04), **_brackets([TaxBracket(upper="Infinity", rate=0.125)]))
    assert tax.tolist() == [1]


def test_ltcg_brackets_zero_when_total_taxable_below_threshold() -> None:
    """Ordinary $30k + LTCG $10k = $40k total. The federal 0% LTCG bracket extends to
    $47,025 so all $10k of LTCG falls in it → zero tax."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    tax = _apply_ltcg_brackets(_quanta(10_000.0), _quanta(30_000.0), **_brackets(fed.ltcg_brackets["single"]))
    assert tax.tolist() == [0]


def test_ltcg_brackets_split_across_zero_and_fifteen_percent() -> None:
    """Ordinary $30k + LTCG $20k = $50k total. The 0% bracket ends at $47,025; LTCG occupies
    $30k-$50k.
    0% rate on $30k-$47,025 = $17,025 → $0.
    15% rate on $47,025-$50,000 = $2,975 → $446.25.
    Total LTCG tax = $446.25."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    tax = _apply_ltcg_brackets(_quanta(20_000.0), _quanta(30_000.0), **_brackets(fed.ltcg_brackets["single"]))
    assert tax.tolist() == [44_625]


def test_ltcg_brackets_pure_fifteen_when_ordinary_already_in_15pct_band() -> None:
    """Ordinary income $100k already past the 0% LTCG threshold. LTCG $50k sits entirely in
    the 15% bracket → $7500."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    tax = _apply_ltcg_brackets(_quanta(50_000.0), _quanta(100_000.0), **_brackets(fed.ltcg_brackets["single"]))
    assert tax.tolist() == [750_000]


def test_ltcg_brackets_vectorized() -> None:
    """One call, four rollouts, each with its own ordinary + LTCG combination — all bracket
    walks share the same schedule."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    tax = _apply_ltcg_brackets(
        _quanta(10_000.0, 20_000.0, 50_000.0, 0.0),
        _quanta(30_000.0, 30_000.0, 100_000.0, 0.0),
        **_brackets(fed.ltcg_brackets["single"]),
    )
    assert tax.tolist() == [0, 44_625, 750_000, 0]


def test_net_capital_gains_pure_gains_pass_through() -> None:
    """With no losses and no carryforward, ST/LT gains are returned untouched and nothing
    offsets ordinary income — the netting must be a no-op for the common all-gains year."""
    net_st, net_lt, offset, carry_out = _net(_quanta(4_000.0), _quanta(10_000.0), _quanta(0.0))
    assert net_st.tolist() == [400_000]
    assert net_lt.tolist() == [1_000_000]
    assert offset.tolist() == [0]
    assert carry_out.tolist() == [0]


def test_net_capital_gains_short_term_loss_offsets_long_term_gain() -> None:
    """A net short-term loss cross-nets against a long-term gain before either is taxed."""
    net_st, net_lt, offset, carry_out = _net(_quanta(-3_000.0), _quanta(10_000.0), _quanta(0.0))
    assert net_st.tolist() == [0]
    assert net_lt.tolist() == [700_000]
    assert offset.tolist() == [0]
    assert carry_out.tolist() == [0]


def test_net_capital_gains_net_loss_caps_ordinary_offset_and_carries_remainder() -> None:
    """A $12k net capital loss deducts $3k against ordinary income this year and carries the
    remaining $9k forward; no gains remain to tax."""
    net_st, net_lt, offset, carry_out = _net(_quanta(-5_000.0), _quanta(-7_000.0), _quanta(0.0))
    assert net_st.tolist() == [0]
    assert net_lt.tolist() == [0]
    assert offset.tolist() == [300_000]
    assert carry_out.tolist() == [900_000]


def test_net_capital_gains_small_loss_fully_offsets_ordinary() -> None:
    """A net loss below the $3k cap is fully deducted with nothing carried forward."""
    _, _, offset, carry_out = _net(_quanta(-1_200.0), _quanta(0.0), _quanta(0.0))
    assert offset.tolist() == [120_000]
    assert carry_out.tolist() == [0]


def test_net_capital_gains_carryforward_consumes_following_year_gain() -> None:
    """A prior-year carryforward offsets this year's gains (short-term first), shrinking the
    taxed gain and the carryforward balance."""
    net_st, net_lt, offset, carry_out = _net(_quanta(2_000.0), _quanta(5_000.0), _quanta(9_000.0))
    # $9k carryforward wipes the $2k ST gain and $5k LT gain (total $7k); the unused $2k is then a
    # net capital loss that deducts (under the $3k cap) against ordinary income, leaving $0 to carry.
    assert net_st.tolist() == [0]
    assert net_lt.tolist() == [0]
    assert offset.tolist() == [200_000]
    assert carry_out.tolist() == [0]


def test_net_capital_gains_vectorized_independent_rollouts() -> None:
    """One call over multiple rollouts: a gain year, a carryforward-consumed year, and a
    net-loss year each resolve independently."""
    net_st, net_lt, offset, carry_out = _net(
        _quanta(4_000.0, 1_000.0, -2_000.0), _quanta(10_000.0, 1_000.0, -6_000.0), _quanta(0.0, 0.0, 0.0)
    )
    assert net_st.tolist() == [400_000, 100_000, 0]
    assert net_lt.tolist() == [1_000_000, 100_000, 0]
    assert offset.tolist() == [0, 0, 300_000]
    assert carry_out.tolist() == [0, 0, 500_000]


def test_tax_math_is_traceable() -> None:
    """All three run under `jax.jit` — they are scan code, so a numpy op creeping in would be
    a defect none of the eager cases above can see."""
    fed = load_jurisdiction("federal_us")
    assert fed.ltcg_brackets is not None
    ordinary_table = _brackets(fed.ordinary_income_brackets["single"])
    ltcg_table = _brackets(fed.ltcg_brackets["single"])
    ordinary_tax = jax.jit(lambda amount: _apply_brackets(amount, **ordinary_table))(_quanta(50_000.0))
    ltcg_tax = jax.jit(lambda gain, ordinary: _apply_ltcg_brackets(gain, ordinary, **ltcg_table))(
        _quanta(20_000.0), _quanta(30_000.0)
    )
    _, _, offset, _ = jax.jit(_net)(_quanta(-5_000.0), _quanta(-7_000.0), _quanta(0.0))
    assert ordinary_tax.tolist() == [605_300]
    assert ltcg_tax.tolist() == [44_625]
    assert offset.tolist() == [300_000]


if __name__ == "__main__":
    pytest_bazel.main()
