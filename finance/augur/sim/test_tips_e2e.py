"""Sim-level e2e for inflation-indexed bonds (TIPS).

A deterministic CPI path, so every assertion is an exact number rather than a band. That
matters most for the phantom income: the whole TIPS-vs-muni question turns on tax owed in
a year with no cash to pay it, and a test that only checked "roughly rises" would not
notice the accretion being off by a year or a factor.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import numpy as np
import polars as pl
import pytest_bazel

from finance.augur.model.deterministic import Deterministic
from finance.augur.model.level_series_groups import IndexSeriesGroups
from finance.augur.model.series import InflationKey
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.sim.compiler.series import scenario_level_series_keys
from finance.augur.sim.engine.jax_engine import run_jax_product_metric_arrays
from finance.augur.sim.scenario import Agent, BondHolding, FilingStatus, InitialAccountBalance, Scenario, TaxProfile
from finance.augur.sim.simulate import simulate
from finance.augur.sim.testing.state_helpers import cash_balances, ordinary_income_ytd

_HORIZON = 14
_FACE = 1_000_000
_RATE = 0.04
# Semiannual on a $1M face at 4% → $20,000 per period BEFORE indexation.
_NOMINAL_COUPON = 20_000

# CPI doubles over month 12, in one step at month 6, and is flat elsewhere. A step rather
# than a drift so the accretion lands in exactly one identifiable month.
_CPI_DOUBLING = [100.0] * 6 + [200.0] * (_HORIZON + 1 - 6)
_CPI_FLAT = [100.0] * (_HORIZON + 1)
# Ends below par, to exercise the deflation floor.
_CPI_DEFLATING = [100.0] * 6 + [80.0] * (_HORIZON + 1 - 6)


def _quanta(amount: float | int) -> int:
    return int(Decimal(str(amount)) / Decimal("0.01"))


def _bundle(levels: list[float]) -> SeriesModelBundle:
    return SeriesModelBundle.independent(index_series=IndexSeriesGroups(inflation=Deterministic(levels=levels)))


def _scenario(*, indexed: bool, cpi: list[float], taxed: bool = False, maturity: int = 12) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=100000),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
        ],
        initial_bonds=[
            BondHolding(
                bond_id="rung",
                agent_id="alice",
                account_id="checking",
                issuer_jurisdiction_id="federal_us",
                face_value=_FACE,
                purchase_price=_FACE,
                annual_coupon_rate=_RATE,
                coupon_period_months=6,
                purchase_month_index=0,
                maturity_month_index=maturity,
                inflation_indexed=indexed,
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ]
        if taxed
        else [],
        external_series=_bundle(cpi),
        horizon_months=_HORIZON,
    )


def _cash_by_month(scenario: Scenario) -> dict[int, int]:
    """Cash change attributable to each month. Row `m + 1` is the balance once month `m`
    has run, so a cashflow in month `m` is the delta into row `m + 1`."""

    run = simulate(scenario, rollout_count=1, locations={})
    balances = [
        int(v)
        for v in cash_balances(run)
        .filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("balance_quanta")
        .to_list()
    ]
    return {month: after - before for month, (before, after) in enumerate(pairwise(balances))}


def _income_by_month(scenario: Scenario) -> list[int]:
    run = simulate(scenario, rollout_count=1, locations={})
    return [
        int(v)
        for v in ordinary_income_ytd(run)
        .filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("ordinary_income_quanta")
        .to_list()
    ]


def test_coupon_rides_the_indexed_principal() -> None:
    """CPI doubles at month 6, so the month-6 coupon is still nominal (indexation is applied
    at the payment, and CPI has only just stepped) and the month-12 coupon is doubled."""

    deltas = _cash_by_month(_scenario(indexed=True, cpi=_CPI_DOUBLING, maturity=120))
    paid = {m: d for m, d in deltas.items() if d}

    assert paid == {6: _quanta(2 * _NOMINAL_COUPON), 12: _quanta(2 * _NOMINAL_COUPON)}


def test_a_nominal_bond_ignores_the_same_cpi_path() -> None:
    """The control. Same terms, same CPI, `inflation_indexed=False` — proves the divergence
    above comes from indexation and not from something else the bundle changed."""

    deltas = _cash_by_month(_scenario(indexed=False, cpi=_CPI_DOUBLING, maturity=120))
    paid = {m: d for m, d in deltas.items() if d}

    assert paid == {6: _quanta(_NOMINAL_COUPON), 12: _quanta(_NOMINAL_COUPON)}


def test_accretion_is_income_with_no_cash_behind_it() -> None:
    """Phantom income, and the reason TIPS lose to munis after tax in some scenarios.

    CPI doubles at month 6, so principal rises $1M that month. That $1M is taxable interest
    the moment it accrues — but no cash moves for it, and month 6's only cash is the coupon.
    """

    # Taxed, because the income tensor only has rows for agents with a tax profile — an
    # untaxed agent accrues nothing to read back.
    income = _income_by_month(_scenario(indexed=True, cpi=_CPI_DOUBLING, taxed=True, maturity=120))
    deltas = _cash_by_month(_scenario(indexed=True, cpi=_CPI_DOUBLING, maturity=120))

    # Year 1 holds ONE coupon (month 6; the month-12 one is next tax year), doubled by the
    # CPI step, plus the full $1M of accretion. Accretion dwarfing the coupon is the point:
    # that is the tax bill a TIPS hands you with no cash attached.
    assert max(income) == _quanta(2 * _NOMINAL_COUPON + _FACE)
    # And month 6 moved only the coupon in cash — the accretion is not there.
    assert deltas[6] == _quanta(2 * _NOMINAL_COUPON)


def test_accretion_is_federally_taxed_and_state_exempt() -> None:
    """Accretion is Treasury interest, so it inherits 31 USC 3124 like a coupon does. If it
    were tagged as ordinary income instead, California would tax it."""

    run = simulate(_scenario(indexed=True, cpi=_CPI_DOUBLING, taxed=True, maturity=120), rollout_count=1, locations={})
    tax = {
        str(r["jurisdiction_id"]): int(r["amount_quanta"])
        for r in run.events_log.tax_accruals.group_by("jurisdiction_id").agg(pl.col("amount_quanta").sum()).to_dicts()
    }

    assert tax["federal_us"] > 0
    assert tax["california"] == 0


def test_redemption_is_floored_at_par_when_prices_fall() -> None:
    """The deflation floor. CPI ends at 80% of its purchase level, so indexed principal is
    $800k — but a TIPS redeems at par, which is the promise that makes it a floor in exactly
    the scenario a floor exists for."""

    deltas = _cash_by_month(_scenario(indexed=True, cpi=_CPI_DEFLATING, maturity=12))
    # Final coupon rides the deflated principal; the principal itself comes back whole.
    assert deltas[12] == _quanta(_FACE + 0.8 * _NOMINAL_COUPON)


def test_a_flat_cpi_makes_a_tips_behave_exactly_like_a_nominal_bond() -> None:
    """Indexation with no inflation must be the identity, not merely close — it is integer
    cents on both paths, so any rounding drift in the indexed branch would show here."""

    indexed = _cash_by_month(_scenario(indexed=True, cpi=_CPI_FLAT, maturity=12))
    nominal = _cash_by_month(_scenario(indexed=False, cpi=_CPI_FLAT, maturity=12))

    assert indexed == nominal


def test_cash_is_still_conserved_with_an_indexed_bond() -> None:
    """The invariant from the double-entry change, applied to the one instrument that
    creates income without cash. If accretion ever reached the cash tensor this breaks."""

    run = simulate(_scenario(indexed=True, cpi=_CPI_DOUBLING, maturity=12), rollout_count=1, locations={})
    state = np.asarray(run.output.state.cash, dtype=np.int64)
    totals = state.sum(axis=tuple(range(1, state.ndim)))

    assert np.all(totals == totals[0])


def test_net_worth_carries_a_tips_at_indexed_principal() -> None:
    """Carrying a TIPS at par would understate net worth by the whole accretion — in exactly
    the inflationary scenarios the ladder is held for, which is the worst place to be wrong.

    CPI doubles at month 6, so after that the $1M bond is worth $2M on the balance sheet.
    Asserted against the nominal control on the same CPI path, which stays at $1M.
    """

    def bond_value(indexed: bool) -> list[int]:
        run = simulate(_scenario(indexed=indexed, cpi=_CPI_DOUBLING, maturity=120), rollout_count=1, locations={})
        arrays = run_jax_product_metric_arrays(run.plan, primary_agent_id="alice").metric_arrays()
        return [int(v) for v in arrays["bond_value_quanta"][:, 0]]

    assert bond_value(indexed=True)[-1] == _quanta(2 * _FACE)
    assert bond_value(indexed=False)[-1] == _quanta(_FACE)


def test_a_tips_demands_an_inflation_path_from_the_exogenous_model() -> None:
    """The demand a TIPS makes that nothing else in a scenario need make.

    Every other level-series demand comes from something PRICED — a lot, a sleeve, a sale. A
    bond has no price series at all, so a TIPS is the one instrument whose exogenous demand is
    invisible from the thing that carries it. Without this, `compile_bonds` raises "carry no
    inflation path" for any caller that derives its sampling request from the scenario — which
    is what the product surface does — unless it happens to want CPI for some other reason.

    Asserted on the DEMAND function rather than through a run, because a run supplies its own
    bundle and would pass either way. That is exactly how the gap stayed invisible from `sim/`.
    """

    indexed = _scenario(cpi=[1] * (_HORIZON + 1), indexed=True)
    nominal = _scenario(cpi=[1] * (_HORIZON + 1), indexed=False)

    assert InflationKey() in scenario_level_series_keys(indexed)
    # And not otherwise: a nominal bond's cashflows are fixed by its terms, so demanding a
    # series it never reads would make an unmodeled-inflation deployment fail for no reason.
    assert InflationKey() not in scenario_level_series_keys(nominal)


if __name__ == "__main__":
    pytest_bazel.main()
