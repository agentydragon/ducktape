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
    Scenario,
    ScheduledPropertyPurchase,
)
from augur.sim.simulate import simulate, simulate_dense_with_external_series

PROFILE_LOCATION_ID = "sf"


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
    parser.add_argument("--sort", choices=["cumulative", "tottime", "both"], default="both")
    parser.add_argument("--top", type=int, default=35)
    parser.add_argument("--no-profile", action="store_true", help="wall-clock only, no cProfile overhead")
    parser.add_argument(
        "--dense-only",
        action="store_true",
        help="run the dense engine (compile + month loop) and skip the Polars decode, to isolate "
        "pure rollout-compute cost/memory from the encode boundary",
    )
    args = parser.parse_args()

    scenario, locations = build_profile_scenario(horizon_months=args.horizon_months)

    def run(rollout_count: int) -> None:
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
            simulate(scenario, rollout_count=rollout_count, locations=locations)

    # Warm-up tiny run to pay one-time import / JIT-ish costs outside the timed region.
    run(2)

    print(f"rollouts={args.rollouts} horizon_months={args.horizon_months} dense_only={args.dense_only}")

    if args.no_profile:
        t0 = time.perf_counter()
        run(args.rollouts)
        print(f"wall_clock_sec={time.perf_counter() - t0:.3f}")
        return

    profiler = cProfile.Profile()
    t0 = time.perf_counter()
    profiler.enable()
    run(args.rollouts)
    profiler.disable()
    elapsed = time.perf_counter() - t0
    print(f"wall_clock_sec={elapsed:.3f}")

    sort_keys = ("cumulative", "tottime") if args.sort == "both" else (args.sort,)
    for sort_key in sort_keys:
        stream = io.StringIO()
        stats = pstats.Stats(profiler, stream=stream).sort_stats(sort_key)
        stats.print_stats(args.top)
        print(f"\n===== top {args.top} by {sort_key} =====")
        print(stream.getvalue())


if __name__ == "__main__":
    main()
