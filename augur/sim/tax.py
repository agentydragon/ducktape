"""Tax computation primitives.

The bracket-walk function (`apply_brackets`) is the only piece of
tax math that doesn't live inline in `step.py` / `apply.py`. It
takes a per-rollout vector of taxable income and a bracket
schedule (already loaded from a jurisdiction YAML) and returns the
per-rollout tax owed. Pure numpy, no scenario / state knowledge.

At spike 1 step 7 only ordinary-income brackets are walked.
Capital-gains-aware bracket math comes at step 8.
"""

from __future__ import annotations

import numpy as np

from augur.sim.jurisdictions import TaxBracket


def apply_brackets(amounts_per_rollout: np.ndarray, brackets: list[TaxBracket]) -> np.ndarray:
    """Walk a marginal-rate schedule across each amount in
    `amounts_per_rollout` and return the per-rollout tax owed.

    Vectorized: each bracket contributes
    `clip(min(amount, upper) - prev_upper, 0, ∞) * rate` to the
    running tax for every rollout simultaneously. The top
    `upper_usd: .inf` bracket catches all remaining income.

    Inputs at or below 0 (e.g. after standard-deduction subtraction)
    produce 0 tax — `clip` clamps the negative slice to 0."""
    tax = np.zeros_like(amounts_per_rollout, dtype=np.float64)
    prev_upper = 0.0
    for bracket in brackets:
        slice_top = np.minimum(amounts_per_rollout, bracket.upper_usd)
        in_bracket = np.maximum(slice_top - prev_upper, 0.0)
        tax += in_bracket * bracket.rate
        prev_upper = bracket.upper_usd
    return tax


def net_capital_gains_with_carryforward(
    short_term: np.ndarray,
    long_term: np.ndarray,
    carryforward_in: np.ndarray,
    *,
    max_ordinary_offset_usd: float = 3000.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Apply simplified §1211/§1212 capital-loss netting for one tax year.

    Per-rollout vectors: `short_term`/`long_term` are this year's net realized ST/LT capital
    gain (signed; negative = a net loss in that category); `carryforward_in` is prior years'
    unused capital loss (>= 0, a loss magnitude). Returns `(net_short_term, net_long_term,
    ordinary_offset, carryforward_out)`:

    - `net_short_term`/`net_long_term` (>= 0): gains that remain to be taxed (short-term at
      ordinary rates, long-term at LTCG rates).
    - `ordinary_offset` (0 .. `max_ordinary_offset_usd`): net capital loss applied against
      ordinary income this year.
    - `carryforward_out` (>= 0): residual loss carried to future years.

    Simplifications vs the IRC: the carryforward is a single pooled magnitude (ST/LT character is
    not preserved across years), it is applied against short-term gains before long-term
    (taxpayer-favorable, ST being taxed at the higher ordinary rate), and — like the bracket
    walks — the offset can exceed ordinary income (no NOL modeling)."""
    st = np.asarray(short_term, dtype=np.float64).copy()
    lt = np.asarray(long_term, dtype=np.float64).copy()

    # Cross-net opposite-sign categories: a net loss in one offsets a net gain in the other.
    st_loss_vs_lt_gain = np.minimum(np.maximum(-st, 0.0), np.maximum(lt, 0.0))
    st += st_loss_vs_lt_gain
    lt -= st_loss_vs_lt_gain
    lt_loss_vs_st_gain = np.minimum(np.maximum(-lt, 0.0), np.maximum(st, 0.0))
    lt += lt_loss_vs_st_gain
    st -= lt_loss_vs_st_gain

    # Consume prior-year carryforward against remaining gains, short-term first.
    carry = np.asarray(carryforward_in, dtype=np.float64).copy()
    used_short_term = np.minimum(np.maximum(st, 0.0), carry)
    st -= used_short_term
    carry -= used_short_term
    used_long_term = np.minimum(np.maximum(lt, 0.0), carry)
    lt -= used_long_term
    carry -= used_long_term

    net_short_term = np.maximum(st, 0.0)
    net_long_term = np.maximum(lt, 0.0)
    residual_loss = np.maximum(-(st + lt), 0.0) + carry
    ordinary_offset = np.minimum(residual_loss, max_ordinary_offset_usd)
    carryforward_out = residual_loss - ordinary_offset
    return net_short_term, net_long_term, ordinary_offset, carryforward_out


def apply_ltcg_brackets(
    ltcg_amount: np.ndarray, ordinary_taxable: np.ndarray, ltcg_brackets: list[TaxBracket]
) -> np.ndarray:
    """Walk a federal-style LTCG bracket schedule where rates depend
    on where the gain sits in the combined ordinary+LTCG stack
    (§1(h)). Conceptually: ordinary income occupies brackets first;
    LTCG fills the brackets above it at the LTCG rates.

    For each bracket, the LTCG slice in it is
    `clip(min(ordinary + ltcg, upper) - max(ordinary, prev_upper),
    0, ∞)`. Vectorized across the rollout dimension."""
    tax = np.zeros_like(ltcg_amount, dtype=np.float64)
    prev_upper = 0.0
    total_taxable = ordinary_taxable + ltcg_amount
    for bracket in ltcg_brackets:
        slice_top = np.minimum(total_taxable, bracket.upper_usd)
        slice_bottom = np.maximum(ordinary_taxable, prev_upper)
        in_bracket = np.maximum(slice_top - slice_bottom, 0.0)
        tax += in_bracket * bracket.rate
        prev_upper = bracket.upper_usd
    return tax
