"""Run the allocation question end to end against the structural macro provider.

*How should I allocate my current assets?* — swept over the equity share, with everything
else held fixed: constant CPI-adjusted spending, one target-allocation policy with a cash
band, and a Californian's tax profile so a muni fund's exemption is worth what it is worth.

Exploratory, hence `x/`. The point is to see what the provider produces once it is wired to
a real portfolio, not to assert anything. Every parameter is fitted now, but fitted is not the
same as complete — the fit windows are a choice and the gaps in <../model/SPEC.md> are things
no fit closes — so read the SHAPE and not the levels. The
shape it produces is the textbook one, which is the encouraging part: at a low withdrawal
rate more equity strictly RAISES P[ruin] (the spend is already covered, so equity adds only
variance); at a high one it strictly lowers it (bonds cannot fund the spend at all, so risk
is the only option); in between the curve is U-shaped. WHERE the minimum sits moves with the
withdrawal rate and with the portfolio, so it is an output of a run and never a default.

Runs remotely — `bazelisk run` locally cannot fetch `rules_mypy` through the egress policy:

    bbr run //finance/augur/x:allocation_sweep
"""

from __future__ import annotations

import numpy as np
import polars as pl

from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.series import InflationKey, SecurityKey, SecuritySymbol
from finance.augur.model.structural_macro import (
    INFLATION_RATE,
    SHORT_RATE,
    TERM_SPREAD,
    EquitySpec,
    InstrumentSpec,
    StructuralMacroProviderConfig,
)
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.external_series import ExternalSeriesContext, materialize_sampled_exogenous
from finance.augur.sim.projections import project_net_worth
from finance.augur.sim.scenario import (
    Agent,
    DistributionTaxSlice,
    FilingStatus,
    InitialAccountBalance,
    InitialLot,
    RecurringObligation,
    Scenario,
    SecurityDistribution,
    SeriesIndexedAmount,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate_with_external_series

HORIZON_MONTHS = 360
ROLLOUTS = 250
# The diagnostics run their own, narrower sample; see `_print_diagnostics`.
DIAGNOSTIC_ROLLOUTS = 60
# Narrower still for the rebalancing arm. Purchase slots are a DENSE lot axis: 180 slots per
# sleeve makes it 362 lots deep, and the per-lot history slab is (lots x rollouts x months).
# 60 rollouts at that depth is an OOM, which is the practical cost of tight rebalancing.
REBALANCE_ROLLOUTS = 8

INITIAL_WEALTH_USD = 2_000_000.0
# Three withdrawal rates, because one is not a study: at 4.8% almost every allocation ruins
# and the table saturates at zero, at 3.0% almost none do, and the shape reverses between
# them. A single rate would have shown one of those three answers and looked conclusive.
MONTHLY_SPENDS_USD = (5_000.0, 6_667.0, 8_000.0)
CASH_FLOOR_USD = 25_000.0
CASH_CEILING_USD = 75_000.0

EQUITY = SecuritySymbol("VOO")
MUNI = SecuritySymbol("CMF")

EQUITY_PRICE_USD = 520.0
MUNI_PRICE_USD = 56.0
MUNI_DURATION_YEARS = 5.5
# Municipals yield LESS than Treasuries pre-tax; that gap is what the exemption buys back.
MUNI_SPREAD = -0.012

# A Californian's muni fund: mostly CA-exempt, the rest fully taxable.
MUNI_TAX_CHARACTER = (
    DistributionTaxSlice(fraction=0.95, issuer_jurisdiction_id="california"),
    DistributionTaxSlice(fraction=0.05),
)

EQUITY_SHARES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

# The "25" of the standard 5/25 rule: rebalance when a sleeve drifts a quarter off its own
# target. Every buy needs its own lot, and lots are a dense axis, so the count is configured
# up front and a run that wants more aborts rather than quietly rebalancing less.
# 0.25 is the "25" of the standard 5/25 rule. The tighter ones are here because 5/25 turns
# out to be nearly a no-op for a spending retiree, and a single tolerance could not show that.
REBALANCE_TOLERANCES = (0.25, 0.10, 0.02)
# Sized for the TIGHTEST tolerance below, which needs ~125. Under-sizing aborts loudly
# rather than quietly rebalancing less, which is how the 40 this started at was caught.
PURCHASE_SLOTS_PER_SLEEVE = 150


def _provider() -> StructuralMacroProviderConfig:
    return StructuralMacroProviderConfig(
        equity=EquitySpec(symbol=EQUITY, initial_price_usd=EQUITY_PRICE_USD),
        instruments=(
            InstrumentSpec(
                symbol=MUNI, duration_years=MUNI_DURATION_YEARS, initial_price_usd=MUNI_PRICE_USD, spread=MUNI_SPREAD
            ),
        ),
    )


def _scenario(
    equity_share: float, monthly_spend_usd: float, *, distributes: bool = True, rebalance: float | None = None
) -> Scenario:
    """The same portfolio at a different split. Only the two lot sizes and the two sleeve
    weights change across the sweep; spending, the cash band and the tax profile do not."""

    lots = [
        InitialLot(
            lot_id=f"{symbol}_initial",
            agent_id="rai",
            account_id="brokerage",
            asset=SecurityKey(symbol=symbol),
            purchase_month_index=-24,
            quantity=INITIAL_WEALTH_USD * share / price,
            cost_basis_per_unit_usd=price,
        )
        for symbol, share, price in (
            (EQUITY, equity_share, EQUITY_PRICE_USD),
            (MUNI, 1.0 - equity_share, MUNI_PRICE_USD),
        )
        if share > 0.0
    ]
    # Weights are integers and only their ratios matter, so the sweep's share becomes
    # percentage points. A zero-weight sleeve is omitted rather than passed as 0 — an asset
    # the policy does not name is outside the target denominator, which is what "no equity"
    # actually means.
    equity_points = round(equity_share * 100)
    sleeves = [
        SleeveTarget(asset=SecurityKey(symbol=symbol), weight=points)
        for symbol, points in ((EQUITY, equity_points), (MUNI, 100 - equity_points))
        if points > 0
    ]

    return Scenario(
        agents=[Agent(agent_id="rai"), Agent(agent_id="world"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="rai", account_id="checking", balance_usd=CASH_CEILING_USD),
            InitialAccountBalance(agent_id="world", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=lots,
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                obligation_id="living_expenses",
                obligation_type="spending",
                agent_id="rai",
                from_account_id="checking",
                to_agent_id="world",
                to_account_id="checking",
                # Constant in REAL terms, which is the whole premise of the question. A
                # nominal-constant spend would make every allocation look survivable.
                amount_due_usd=SeriesIndexedAmount(
                    base_amount_usd=monthly_spend_usd, series=InflationKey(), adjustment_period_months=12
                ),
            )
        ],
        # Declared only when the sleeve exists: the compiler rejects a payout on a pool that
        # holds no lots and has no purchase slot, on the grounds that it would be silently
        # zero for the whole horizon. Right call, and it means the 100%-equity arm declares
        # nothing rather than declaring something inert.
        security_distributions=[
            SecurityDistribution(
                asset=SecurityKey(symbol=MUNI),
                agent_id="rai",
                holding_account_id="brokerage",
                to_account_id="checking",
                tax_character=MUNI_TAX_CHARACTER,
            )
        ]
        if distributes and equity_share < 1.0
        else [],
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id="rai",
                account_id="checking",
                source_account_ids=("brokerage",),
                sleeves=sleeves,
                cash_floor_usd=CASH_FLOOR_USD,
                cash_ceiling_usd=CASH_CEILING_USD,
                # Without these, "60% equity" means 60% AT MONTH ZERO and whatever drift makes
                # of it over thirty years — the equity sleeve compounds away from its target and
                # is only ever trimmed when cash is needed. That is a real strategy, but it is
                # not the 60/40 anyone means, and the difference is what this arm measures.
                rebalance_tolerance=rebalance,
                purchase_slots_per_sleeve=PURCHASE_SLOTS_PER_SLEEVE if rebalance is not None else 0,
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="rai",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=HORIZON_MONTHS,
    )


def _sample(rollout_count: int) -> ExternalSeriesContext:
    return materialize_sampled_exogenous(
        _provider()
        .realize_model()
        .sample(ExogenousSamplingRequest(horizon_months=HORIZON_MONTHS, rollout_seeds=tuple(range(rollout_count))))
    )


def _run(scenario: Scenario, external: ExternalSeriesContext, rollout_count: int) -> SimulationRun:
    return simulate_with_external_series(scenario, rollout_count=rollout_count, external_series=external, locations={})


def _first_failure_month(run: SimulationRun, rollout_count: int) -> np.ndarray:
    """Month each rollout first could not pay the spend from cash; `HORIZON_MONTHS` if never.

    Ruin here means exactly "the spend could not be met", not "the balance hit zero" — those
    differ, and the engine records the first one. Survival TIME rather than a ruin flag,
    because at a high withdrawal rate every allocation ruins and a flag stops discriminating
    exactly where the comparison gets interesting.
    """

    survived = np.full(rollout_count, HORIZON_MONTHS, dtype=np.float64)
    failures = run.events_log.rollout_failures
    if failures.is_empty():
        return survived
    first = failures.group_by("rollout_index").agg(pl.col("month_index").min())
    survived[first.get_column("rollout_index").to_numpy()] = first.get_column("month_index").to_numpy()
    return survived


def _ruin_fraction(run: SimulationRun, rollout_count: int) -> float:
    return float(np.mean(_first_failure_month(run, rollout_count) < HORIZON_MONTHS))


def _equity_share_by_month(run: SimulationRun) -> dict[int, float]:
    """Median equity share of marked lot value, by month. Cash is excluded — the cash band is
    a separate mechanism and folding it in would blur the sleeve drift this measures."""

    priced = run.asset_lots.join(
        run.series_values,
        left_on=["rollout_index", "month_index", "asset_id"],
        right_on=["rollout_index", "month_index", "series_id"],
        how="left",
    ).with_columns(value_usd=pl.col("remaining_quantity") * pl.col("value").fill_null(0.0))
    by_rollout_month = priced.group_by(["rollout_index", "month_index"]).agg(
        equity_usd=pl.col("value_usd").filter(pl.col("asset_id") == f"security:{EQUITY}").sum(),
        total_usd=pl.col("value_usd").sum(),
    )
    median = (
        by_rollout_month.filter(pl.col("total_usd") > 0.0)
        .with_columns(share=pl.col("equity_usd") / pl.col("total_usd"))
        .group_by("month_index")
        .agg(pl.col("share").median())
    )
    return dict(zip(median.get_column("month_index").to_list(), median.get_column("share").to_list(), strict=True))


def _terminal_liquid_usd(run: SimulationRun) -> np.ndarray:
    """Cash plus marked lot value at the final month, per rollout. Nominal, not real."""

    return (
        project_net_worth(run)
        .filter((pl.col("agent_id") == "rai") & (pl.col("month_index") == HORIZON_MONTHS))
        .sort("rollout_index")
        .get_column("liquid_net_worth_usd")
        .to_numpy()
    )


def _print_parameter_summary(config: StructuralMacroProviderConfig) -> None:
    """What the parameters actually imply, so the levels below can be judged against something.

    Every block is fitted now, so the defence this printout provides has changed: the risk is
    no longer a hand-set number read as a finding, it is FITTED numbers read as complete. What
    the model structurally cannot do — no equity/rates coupling, no held-to-maturity
    instrument — does not show up anywhere in the table below, so it is printed above it.
    """

    equity = config.equity
    assert equity is not None
    nominal = float(np.expm1(equity.monthly_log_return_mu * 12))
    transition = np.asarray(config.macro_state.transition)
    long_run = np.linalg.solve(np.eye(3) - transition, np.asarray(config.macro_state.intercept))
    initial = config.macro_state.initial_state
    print(
        f"macro state: joint VAR(1) on (short rate, term spread, inflation), fitted on FRED "
        f"FEDFUNDS/GS10/CPIAUCSL 1955-2026.\n"
        f"  today {initial[SHORT_RATE]:.2%} / {initial[TERM_SPREAD]:+.2%} / "
        f"{initial[INFLATION_RATE]:.2%}   long-run {long_run[SHORT_RATE]:.2%} / "
        f"{long_run[TERM_SPREAD]:+.2%} / {long_run[INFLATION_RATE]:.2%}\n"
        f"  inflation persistence {transition[INFLATION_RATE][INFLATION_RATE]:.3f}, Fed "
        f"pass-through {transition[SHORT_RATE][INFLATION_RATE] / (1 - transition[SHORT_RATE][SHORT_RATE]):.2f}\n"
        f"equity: VFINX 1980-2026, {nominal:.1%}/yr nominal at "
        f"{np.sqrt(12) * equity.monthly_log_return_sigma:.1%} vol. Equity and rates are "
        f"INDEPENDENT (SPEC.md § Gaps); the bond sleeve is a marked fund, not a held ladder.\n"
    )


def main() -> None:
    config = _provider()
    print(f"structural_macro | {ROLLOUTS} rollouts x {HORIZON_MONTHS} months")
    print(f"start ${INITIAL_WEALTH_USD:,.0f}, CA muni fund + broad equity, spend constant in REAL terms")
    _print_parameter_summary(config)

    external = _sample(ROLLOUTS)
    for monthly_spend_usd in MONTHLY_SPENDS_USD:
        annual_rate = monthly_spend_usd * 12 / INITIAL_WEALTH_USD
        print(f"\n--- ${monthly_spend_usd:,.0f}/mo = {annual_rate:.1%}/yr of the starting portfolio ---")
        print(f"{'equity':>7} {'P[ruin]':>8} {'p10 terminal':>16} {'median terminal':>18} {'p90 terminal':>16}")
        for equity_share in EQUITY_SHARES:
            run = _run(_scenario(equity_share, monthly_spend_usd), external, ROLLOUTS)
            terminal = _terminal_liquid_usd(run)
            print(
                f"{equity_share:>7.0%} {_ruin_fraction(run, ROLLOUTS):>8.1%}"
                f" ${np.percentile(terminal, 10):>15,.0f}"
                f" ${np.percentile(terminal, 50):>17,.0f}"
                f" ${np.percentile(terminal, 90):>15,.0f}"
            )

    _print_diagnostics()


def _print_diagnostics() -> None:
    """Three checks on whether the wiring is doing what the table implies it is.

    Their own smaller sample: each `SimulationRun` holds dense per-lot history, and the
    rebalancing arm's purchase slots deepen the lot axis by two orders of magnitude, so
    running these at the sweep's width is what an OOM looks like.
    """

    external = _sample(DIAGNOSTIC_ROLLOUTS)
    spend = max(MONTHLY_SPENDS_USD)

    # 1. Does the fixed-income sleeve actually pay? A dropped payout produces a perfectly
    # plausible projection, just one where bonds have no yield — so the check has to be a
    # difference against the same scenario, not a plausibility read on the levels above.
    # Survival months, because at this spend a terminal-wealth comparison is zero against zero.
    with_payout = _first_failure_month(_run(_scenario(0.0, spend), external, DIAGNOSTIC_ROLLOUTS), DIAGNOSTIC_ROLLOUTS)
    without = _first_failure_month(
        _run(_scenario(0.0, spend, distributes=False), external, DIAGNOSTIC_ROLLOUTS), DIAGNOSTIC_ROLLOUTS
    )
    print(
        f"\nall-bond sleeve at ${spend:,.0f}/mo, median months survived:"
        f" {np.median(with_payout):.0f} with the declared payout, {np.median(without):.0f} without it"
    )

    # 2. Did the rebalance fire, and does the tolerance matter? Measured as the equity share
    # over time, NOT as a count of `asset_purchases`: that frame carries scheduled purchases
    # only and is empty for policy-driven buys, so counting it reported a confident zero for
    # every tolerance including one that provably fired (it demanded 123 purchase slots).
    middle = MONTHLY_SPENDS_USD[1]
    narrow = _sample(REBALANCE_ROLLOUTS)
    months = (0, 120, 240, 359)
    print(f"\n60/40 at ${middle:,.0f}/mo: median equity share of marked lots over 30y")
    print(f"  ({REBALANCE_ROLLOUTS} rollouts — purchase slots are a dense lot axis; see REBALANCE_ROLLOUTS)")
    for tolerance in (None, *REBALANCE_TOLERANCES):
        shares = _equity_share_by_month(_run(_scenario(0.6, middle, rebalance=tolerance), narrow, REBALANCE_ROLLOUTS))
        label = "no trigger" if tolerance is None else f"tolerance {tolerance:.0%}"
        drift = "  ".join(f"m{month}: {shares[month]:.0%}" for month in months if month in shares)
        print(f"  {label:>14}:  {drift}")


if __name__ == "__main__":
    main()
