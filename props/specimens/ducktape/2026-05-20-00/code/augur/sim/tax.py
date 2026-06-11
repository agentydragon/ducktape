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
