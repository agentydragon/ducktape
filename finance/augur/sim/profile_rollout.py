"""Standalone rollout profiler for the augur dense-array sim engine.

Builds a "nontrivial knob config" on top of the spike-1 bench scenario:
  - W-2 income, rent, 3-asset portfolio, liquidity policy, CA+federal tax
    (everything `build_bench_scenario` already wires), PLUS
  - a financed primary-residence purchase (mortgage origination + monthly
    amortization), property-tax carrying cost, and a mortgage-interest
    deduction policy.

Runs `simulate(...)` (or just the dense engine, with `--dense-only`) under
cProfile and prints the hottest call sites by cumulative and/or total (self)
time (see `--sort`).

Invoke locally:
  bazelisk run //finance/augur/sim:profile_rollout -- --rollouts 4000 --horizon-months 1200
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import time

import jax

from finance.augur.model.series import SP500Key
from finance.augur.sim.bench_scenario import build_bench_scenario
from finance.augur.sim.external_series import materialize_external_series
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    PrimaryResidenceAssignment,
    PropertyTaxPolicy,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledPropertyPurchase,
)
from finance.augur.sim.simulate import simulate, simulate_with_external_series

PROFILE_LOCATION_ID = "sf"


def _add_scale_sales(scenario: Scenario, *, n: int, horizon_months: int) -> Scenario:
    """Append `n` independent (distinct-account) SP500 lots + scheduled sales, to scale the unrolled
    per-sale loop and measure its compile/execute cost."""
    if n <= 0:
        return scenario
    lots = [
        InitialLot(
            lot_id=f"scale_lot_{i}",
            agent_id="alice",
            account_id=f"scale_brk_{i}",
            asset=SP500Key(),
            purchase_month_index=-24,
            quantity=100.0,
            cost_basis_per_unit_usd=80.0,
        )
        for i in range(n)
    ]
    sales = [
        ScheduledAssetSale(
            month=1 + (i % max(1, horizon_months - 2)),
            cause_id=f"scale_sale_{i}",
            agent_id="alice",
            source_account_id=f"scale_brk_{i}",
            asset=SP500Key(),
            quantity=100.0,
            price_per_unit_usd=120.0,
            proceeds_account_id="checking",
        )
        for i in range(n)
    ]
    return scenario.model_copy(
        update={
            "initial_lots": [*scenario.initial_lots, *lots],
            "scheduled_asset_sales": [*scenario.scheduled_asset_sales, *sales],
        }
    )


def build_transfers_only_scenario(*, horizon_months: int) -> tuple[Scenario, dict[str, Location]]:
    """A transfers-only scenario (recurring paycheck) — exercises the jitted lax.scan fast path."""
    scenario = Scenario(
        agents=[Agent(agent_id="payroll"), Agent(agent_id="alice")],
        initial_cash=[
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=horizon_months - 1,
                cause_id="paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=8_000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )
    return scenario, {}


def build_profile_scenario(*, horizon_months: int) -> tuple[Scenario, dict[str, Location]]:
    """Bench scenario + a financed primary residence with property tax and MID."""
    base = build_bench_scenario(horizon_months=horizon_months)

    extra_agents = [Agent(agent_id="lender"), Agent(agent_id="property_seller")]
    extra_cash = [
        InitialAccountBalance(agent_id="lender", account_id="checking", balance_usd=0.0),
        InitialAccountBalance(agent_id="property_seller", account_id="checking", balance_usd=0.0),
    ]
    purchase = ScheduledPropertyPurchase(
        month=0,
        cause_id="alice_home_purchase",
        property_id="home",
        location_id=PROFILE_LOCATION_ID,
        buyer_agent_id="alice",
        buyer_account_id="checking",
        seller_agent_id="property_seller",
        purchase_price_usd=1_200_000.0,
        down_payment_usd=240_000.0,
        buyer_closing_cost_usd=12_000.0,
        rented_fraction=0.0,
        mortgage=MortgageFinancing(
            liability_id="alice_mortgage",
            lender_agent_id="lender",
            principal_usd=960_000.0,
            annual_interest_rate=0.065,
            term_months=360,
        ),
    )
    scenario = base.model_copy(
        update={
            "agents": [*base.agents, *extra_agents],
            "initial_cash": [*base.initial_cash, *extra_cash],
            "scheduled_property_purchases": [purchase],
            "initial_primary_residences": [PrimaryResidenceAssignment(agent_id="alice", property_id="home")],
            "property_tax_policies": [
                PropertyTaxPolicy(property_id="home", owner_agent_id="alice", tax_authority_agent_id="irs")
            ],
            "mortgage_interest_deduction_policies": [
                MortgageInterestDeductionPolicy(liability_id="alice_mortgage", owner_agent_id="alice")
            ],
        }
    )
    locations = {
        PROFILE_LOCATION_ID: Location(
            location_id=PROFILE_LOCATION_ID,
            display_name="San Francisco",
            jurisdiction_ids=["federal_us", "california"],
            annual_property_tax_rate=0.0118,
        )
    }
    return scenario, locations


def main() -> None:
    parser = argparse.ArgumentParser(description="augur rollout profiler")
    parser.add_argument("--rollouts", type=int, default=4000)
    parser.add_argument("--horizon-months", type=int, default=1200)
    parser.add_argument(
        "--transfers-only",
        action="store_true",
        help="use a transfers-only scenario (routes through the jitted lax.scan fast path on JAX)",
    )
    parser.add_argument("--sort", choices=["cumulative", "tottime", "both"], default="both")
    parser.add_argument("--top", type=int, default=35)
    parser.add_argument("--no-profile", action="store_true", help="wall-clock only, no cProfile overhead")
    parser.add_argument(
        "--trace-out", default=None, help="capture a JAX/XLA perfetto execution trace to this dir (jax backend)"
    )
    parser.add_argument(
        "--repeat-timed",
        type=int,
        default=0,
        help="time N back-to-back runs (no warmup) to expose per-call recompilation cost",
    )
    parser.add_argument(
        "--extra-sales",
        type=int,
        default=0,
        help="append N independent SP500 lots + scheduled sales, to scale the unrolled per-sale loop",
    )
    parser.add_argument(
        "--dense-only",
        action="store_true",
        help="run the dense engine (compile + month loop) and skip the Polars decode, to isolate "
        "pure rollout-compute cost/memory from the encode boundary",
    )
    parser.add_argument(
        "--materialize",
        choices=["all", "rollout", "none"],
        default="all",
        help="which lazy SimulationRun frames to force-decode: 'all' (every frame), 'rollout' "
        "(only what the single-rollout detail view reads: events_log + asset_lots), or 'none' "
        "(decode nothing — the lazy default)",
    )
    args = parser.parse_args()

    scenario, locations = (
        build_transfers_only_scenario(horizon_months=args.horizon_months)
        if args.transfers_only
        else build_profile_scenario(horizon_months=args.horizon_months)
    )
    scenario = _add_scale_sales(scenario, n=args.extra_sales, horizon_months=args.horizon_months)

    def _materialize(result: object) -> None:
        if args.materialize == "none":
            return
        if args.materialize == "rollout":
            _ = result.events_log  # type: ignore[attr-defined]
            _ = result.asset_lots  # type: ignore[attr-defined]
            return
        for frame in (
            "cash_balances",
            "asset_lots",
            "ordinary_income_ytd",
            "capital_gains_ytd",
            "tax_liabilities",
            "property_state",
            "property_stakes",
            "liabilities",
            "rollout_status_history",
            "rollout_status",
            "series_values",
            "events_log",
        ):
            getattr(result, frame)

    def run(rollout_count: int) -> None:
        if args.dense_only:
            external_series = materialize_external_series(
                scenario.external_series,
                rollout_seeds=tuple(range(rollout_count)),
                horizon_months=int(scenario.horizon_months),
            )
            simulate_with_external_series(
                scenario, rollout_count=rollout_count, external_series=external_series, locations=locations
            )
        else:
            _materialize(simulate(scenario, rollout_count=rollout_count, locations=locations))

    def timed() -> float:
        # Warm up at the SAME rollout count so one-time costs (imports, tracing, and especially the
        # JAX/XLA compile, which is shape-specialized on rollout_count) are paid outside the timer.
        run(args.rollouts)
        t0 = time.perf_counter()
        run(args.rollouts)
        return time.perf_counter() - t0

    if args.repeat_timed:
        for i in range(args.repeat_timed):
            t0 = time.perf_counter()
            run(args.rollouts)
            print(f"run[{i}] wall_clock_sec={time.perf_counter() - t0:.3f}")
        return

    print(
        f"rollouts={args.rollouts} horizon_months={args.horizon_months} "
        f"dense_only={args.dense_only} materialize={args.materialize}"
    )

    if args.trace_out is not None:
        run(args.rollouts)  # warm up / compile outside the trace
        with jax.profiler.trace(args.trace_out, create_perfetto_trace=True):
            run(args.rollouts)
        print(f"trace written to {args.trace_out}")
        return

    if args.no_profile:
        print(f"wall_clock_sec={timed():.3f}")
        return

    run(2)  # warm-up outside the profiled region
    profiler = cProfile.Profile()
    t0 = time.perf_counter()
    profiler.enable()
    run(args.rollouts)
    profiler.disable()
    print(f"wall_clock_sec={time.perf_counter() - t0:.3f}")

    sort_keys = ("cumulative", "tottime") if args.sort == "both" else (args.sort,)
    for sort_key in sort_keys:
        stream = io.StringIO()
        stats = pstats.Stats(profiler, stream=stream).sort_stats(sort_key)
        stats.print_stats(args.top)
        print(f"\n===== top {args.top} by {sort_key} =====")
        print(stream.getvalue())


if __name__ == "__main__":
    main()
