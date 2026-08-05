"""TIPS ladder vs municipal ladder, after tax. The question the fixed-income work is for.

Two scenarios identical in every respect except the bond sleeve, held by the same California
resident with the same tax profile over the same stochastic inflation paths:

- a **TIPS** ladder: principal indexed to CPI; coupon and accretion are federal interest,
  state-exempt under 31 USC 3124. The accretion is PHANTOM income — taxed in the year it
  accrues with no cash attached.
- a **California municipal** ladder: nominal principal, coupon exempt federally (IRC 103)
  and by California (own issue). No phantom income, and no inflation protection.

The trade is the whole question. TIPS defend purchasing power but are taxed on gains that
produce no cash; munis pay less and lose to inflation but are taxed nowhere. Which wins is
an empirical question about the inflation path, which is why it is simulated rather than
argued.

This is an EXPERIMENT, not a product assertion — it lives under `x/`. It pins the
comparison's mechanics (both sleeves priced, both taxed correctly, the gap driven by
inflation) so the numbers can be trusted; it deliberately does not assert which instrument
wins, because that depends on the calibrated inflation model and is the thing to look at
rather than freeze.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest_bazel
from numpy.typing import NDArray

from finance.augur.model.gbm import GeometricBrownian
from finance.augur.model.level_series_groups import IndexSeriesGroups
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.product.decode import monthly_metric_arrays_batch
from finance.augur.sim.scenario import Agent, BondHolding, FilingStatus, InitialAccountBalance, Scenario, TaxProfile
from finance.augur.sim.simulate import simulate

_HORIZON = 121  # ten years plus the December that settles the tenth year's tax
_RUNGS = 10
_RUNG_FACE = 100_000.0
_ROLLOUTS = 24

# A TIPS yields less than a nominal bond of the same maturity because the holder is paid in
# inflation protection; a muni yields less than a Treasury because the holder is paid in tax
# exemption. These are illustrative, not fitted — the point of the harness is that the
# comparison responds to them, so they are the first thing to vary.
_TIPS_REAL_RATE = 0.02
_MUNI_RATE = 0.035

# ~2.5%/yr expected inflation with real dispersion, so the paths disagree about whether
# indexation or tax exemption wins.
_INFLATION = GeometricBrownian(initial_value=100.0, monthly_log_return_mu=0.00206, monthly_log_return_sigma=0.004)


def _ladder(*, indexed: bool) -> list[BondHolding]:
    """Ten annual rungs. Maturities are staggered so the ladder rolls, which is what makes
    it a ladder rather than one bond — and what a real floor is built from."""

    return [
        BondHolding(
            bond_id=f"rung_{year}",
            agent_id="rai",
            account_id="checking",
            issuer_jurisdiction_id="federal_us" if indexed else "california",
            face_value_usd=_RUNG_FACE,
            purchase_price_usd=_RUNG_FACE,
            annual_coupon_rate=_TIPS_REAL_RATE if indexed else _MUNI_RATE,
            coupon_period_months=6,
            purchase_month_index=0,
            maturity_month_index=12 * year,
            inflation_indexed=indexed,
        )
        for year in range(1, _RUNGS + 1)
    ]


def _scenario(*, indexed: bool) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="rai"), Agent(agent_id="irs")],
        initial_cash=[
            # Enough to pay the phantom-income tax without forcing a sale, which is the
            # comparison we want. A thinner buffer turns this into a liquidity test instead.
            InitialAccountBalance(agent_id="rai", account_id="checking", balance_usd=200_000.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_bonds=_ladder(indexed=indexed),
        tax_profiles=[
            TaxProfile(
                agent_id="rai",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        external_series=SeriesModelBundle.independent(index_series=IndexSeriesGroups(inflation=_INFLATION)),
        horizon_months=_HORIZON,
    )


def _terminal_net_worth(*, indexed: bool) -> NDArray[np.float64]:
    run = simulate(_scenario(indexed=indexed), rollout_count=_ROLLOUTS, locations={})
    series: NDArray[np.float64] = np.asarray(
        monthly_metric_arrays_batch(run, primary_agent_id="rai")["net_worth_usd"], dtype=np.float64
    )
    return np.asarray(series[-1], dtype=np.float64)


def test_both_ladders_are_priced_and_taxed() -> None:
    """The harness works at all: both sleeves produce terminal wealth on every rollout, and
    neither collapses to zero (which is what a failed rollout or an unpriced bond looks
    like)."""

    tips = _terminal_net_worth(indexed=True)
    muni = _terminal_net_worth(indexed=False)

    assert tips.shape == muni.shape == (_ROLLOUTS,)
    assert np.all(tips > 0)
    assert np.all(muni > 0)


def test_only_the_tips_ladder_responds_to_inflation() -> None:
    """The mechanism the comparison rests on.

    A TIPS ladder's terminal wealth must vary across inflation paths — that is what
    indexation IS. A nominal muni ladder's must not: its cashflows are fixed, so every
    rollout ends identically regardless of what CPI did. If the muni sleeve varied, the
    scenarios would be differing in something other than the instrument.
    """

    assert _terminal_net_worth(indexed=True).std() > 0.0
    assert _terminal_net_worth(indexed=False).std() == 0.0


def test_the_muni_ladder_pays_no_tax_at_all() -> None:
    """The muni side of the trade, and the thing a TIPS cannot match: a California resident
    holding California paper owes nothing to either authority (IRC 103 plus own-issue), so
    the ladder's entire coupon is spendable."""

    run = simulate(_scenario(indexed=False), rollout_count=1, locations={})

    assert run.events_log.tax_accruals.get_column("amount_usd").sum() == 0.0


def test_the_tips_ladder_is_taxed_federally_and_not_by_california() -> None:
    """The TIPS side. Federal tax is owed on coupon AND accretion; California gets nothing,
    because a Treasury is state-exempt however its principal is computed."""

    run = simulate(_scenario(indexed=True), rollout_count=1, locations={})
    by_jurisdiction = {
        str(r["jurisdiction_id"]): float(r["amount_usd"])
        for r in run.events_log.tax_accruals.group_by("jurisdiction_id").agg(pl.col("amount_usd").sum()).to_dicts()
    }

    assert by_jurisdiction["federal_us"] > 0.0
    assert by_jurisdiction.get("california", 0.0) == 0.0


if __name__ == "__main__":
    pytest_bazel.main()
