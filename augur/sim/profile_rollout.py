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
  bazelisk run //augur/sim:profile_rollout -- --rollouts 4000 --horizon-months 1200
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import time

from augur.model.sim_backend import SimBackend, use_backend
from augur.sim.bench_scenario import build_bench_scenario
from augur.sim.external_series import materialize_external_series
from augur.sim.locations import Location
from augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    PrimaryResidenceAssignment,
    PropertyTaxPolicy,
    RecurringTransfer,
    Scenario,
    ScheduledPropertyPurchase,
)
from augur.sim.simulate import simulate, simulate_dense_with_external_series

PROFILE_LOCATION_ID = "sf"


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
        "--backend",
        choices=[*[b.value for b in SimBackend], "both"],
        default="both",
        help="which sim backend(s) to run: a single backend, or 'both' for a numpy-vs-jax comparison",
    )
    parser.add_argument(
        "--transfers-only",
        action="store_true",
        help="use a transfers-only scenario (routes through the jitted lax.scan fast path on JAX)",
    )
    parser.add_argument("--sort", choices=["cumulative", "tottime", "both"], default="both")
    parser.add_argument("--top", type=int, default=35)
    parser.add_argument("--no-profile", action="store_true", help="wall-clock only, no cProfile overhead")
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

    def run(rollout_count: int, backend: SimBackend) -> None:
        with use_backend(backend):
            if args.dense_only:
                external_series = materialize_external_series(
                    scenario.external_series,
                    rollout_seeds=tuple(range(rollout_count)),
                    horizon_months=int(scenario.horizon_months),
                )
                simulate_dense_with_external_series(
                    scenario, rollout_count=rollout_count, external_series=external_series, locations=locations
                )
            else:
                _materialize(simulate(scenario, rollout_count=rollout_count, locations=locations))

    def timed(backend: SimBackend) -> float:
        # Warm up at the SAME rollout count so one-time costs (imports, tracing, and especially the
        # JAX/XLA compile, which is shape-specialized on rollout_count) are paid outside the timer.
        run(args.rollouts, backend)
        t0 = time.perf_counter()
        run(args.rollouts, backend)
        return time.perf_counter() - t0

    print(
        f"rollouts={args.rollouts} horizon_months={args.horizon_months} "
        f"dense_only={args.dense_only} materialize={args.materialize}"
    )

    if args.backend == "both":
        numpy_sec = timed(SimBackend.NUMPY)
        jax_sec = timed(SimBackend.JAX)
        print(f"numpy_wall_clock_sec={numpy_sec:.3f}")
        print(f"jax_wall_clock_sec={jax_sec:.3f}")
        faster, slower = ("jax", "numpy") if jax_sec < numpy_sec else ("numpy", "jax")
        print(
            f"faster={faster} speedup={max(numpy_sec, jax_sec) / min(numpy_sec, jax_sec):.2f}x ({slower} is the baseline)"
        )
        return

    backend = SimBackend(args.backend)
    if args.no_profile:
        print(f"backend={backend.value} wall_clock_sec={timed(backend):.3f}")
        return

    run(2, backend)  # warm-up outside the profiled region
    profiler = cProfile.Profile()
    t0 = time.perf_counter()
    profiler.enable()
    run(args.rollouts, backend)
    profiler.disable()
    print(f"backend={backend.value} wall_clock_sec={time.perf_counter() - t0:.3f}")

    sort_keys = ("cumulative", "tottime") if args.sort == "both" else (args.sort,)
    for sort_key in sort_keys:
        stream = io.StringIO()
        stats = pstats.Stats(profiler, stream=stream).sort_stats(sort_key)
        stats.print_stats(args.top)
        print(f"\n===== top {args.top} by {sort_key} =====")
        print(stream.getvalue())


if __name__ == "__main__":
    main()
