"""Microbenchmark: simulate_set + response materialization wall time.

Workload mirrors a gaffer-private production load:
    15 scenarios x 128 rollouts x 360 months, seed=11.

Times:
  * simulate_set(...) end-to-end
  * arrays.monthly_columns / terminal_columns / metric_fan_columns per scenario
  * total wall

Repeats N times and reports the minimum wall time.
"""

from __future__ import annotations

import time
from pathlib import Path

from augur.core.api import simulate_set
from augur.core.local_regulation import LocalRegulation
from augur.core.market_bundle_test_support import NoopMarketBundleProvider
from augur.core.portfolio import load_portfolio_yaml
from augur.core.scenario_engine import ReportMetric  # noqa: F401 (only to assure module loads)
from augur.core.scenario_set import (
    Actor,
    ActorRole,
    Financing,
    FinancingMode,
    MarketRequest,
    PropertySelection,
    Scenario,
    ScenarioSet,
    TaxRegime,
    TransactionCosts,
)

_PORTFOLIO_EXAMPLE_YAML = Path(__file__).resolve().parent / "testdata" / "portfolio.example.yaml"
if not _PORTFOLIO_EXAMPLE_YAML.exists():
    for candidate in [
        Path("augur/core/testdata/portfolio.example.yaml"),
        Path.cwd() / "augur" / "core" / "testdata" / "portfolio.example.yaml",
    ]:
        if candidate.exists():
            _PORTFOLIO_EXAMPLE_YAML = candidate
            break
    else:
        raise SystemExit(f"could not find portfolio.example.yaml; tried {_PORTFOLIO_EXAMPLE_YAML} and cwd={Path.cwd()}")


_LOCAL_REGULATION_BY_ID: dict[str, LocalRegulation] = {
    "san_francisco_ca": LocalRegulation(
        property_tax_regime=TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
            TaxRegime.SAN_FRANCISCO_TRANSFER_TAX,
        ),
        property_tax_annual_pct=1.18,
        local_transfer_tax_pct=0,
        special_assessment_annual_usd=0,
        notes="bench",
    ),
    "vallejo_ca": LocalRegulation(
        property_tax_regime=TaxRegime.VALLEJO_PROPERTY_TAX,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.VALLEJO_PROPERTY_TAX,
        ),
        property_tax_annual_pct=1.1,
        local_transfer_tax_pct=0,
        special_assessment_annual_usd=0,
        notes="bench",
    ),
    "mare_island_vallejo_ca": LocalRegulation(
        property_tax_regime=TaxRegime.MARE_ISLAND_SPECIAL_ASSESSMENTS,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.MARE_ISLAND_SPECIAL_ASSESSMENTS,
        ),
        property_tax_annual_pct=2.4,
        local_transfer_tax_pct=0,
        special_assessment_annual_usd=0,
        notes="bench",
    ),
}

_LOCATIONS = ("san_francisco_ca", "vallejo_ca", "mare_island_vallejo_ca")
_PURCHASE_PRICES = (350_000.0, 500_000.0, 750_000.0, 900_000.0, 1_250_000.0)
_DOWN_PAYMENT_PCTS = (20.0, 25.0, 30.0)
_MORTGAGE_RATE_PCTS = (5.5, 6.0, 6.5, 7.0)


def build_scenarios(n: int) -> tuple[Scenario, ...]:
    portfolio = load_portfolio_yaml(_PORTFOLIO_EXAMPLE_YAML)
    base_balance_sheet = portfolio.to_initial_balance_sheet()
    actor = Actor(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER)

    scenarios: list[Scenario] = []
    for i in range(n):
        loc = _LOCATIONS[i % len(_LOCATIONS)]
        price = _PURCHASE_PRICES[i % len(_PURCHASE_PRICES)]
        dp = _DOWN_PAYMENT_PCTS[i % len(_DOWN_PAYMENT_PCTS)]
        rate = _MORTGAGE_RATE_PCTS[i % len(_MORTGAGE_RATE_PCTS)]
        scenarios.append(
            Scenario(
                scenario_id=f"bench_{i:02d}",
                label=f"Bench Scenario {i:02d} ({loc})",
                actors=(actor,),
                property_selection=PropertySelection(
                    property_id=f"bench_property_{i:02d}", location_id=loc, purchase_price_usd=price
                ),
                financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=dp, mortgage_rate_pct=rate),
                transaction_costs=TransactionCosts(closing_cost_buy_pct=2.5, closing_cost_sell_pct=0),
                initial_balance_sheet=base_balance_sheet,
            )
        )
    return tuple(scenarios)


def run_once(scenario_set: ScenarioSet) -> tuple[float, float]:
    provider = NoopMarketBundleProvider()
    t0 = time.perf_counter()
    run = simulate_set(scenario_set, market_provider=provider, local_regulation_by_id=_LOCAL_REGULATION_BY_ID)
    sim_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    for scenario_run in run.scenario_runs:
        arrays = scenario_run.arrays
        if arrays is None:
            continue
        _ = arrays.monthly_columns()
        _ = arrays.terminal_columns()
        _ = arrays.metric_fan_columns()
    materialize_elapsed = time.perf_counter() - t0
    return sim_elapsed, materialize_elapsed


def main() -> None:
    n_scenarios = 3
    rollout_count = 32
    horizon_months = 360
    seed = 11
    repeats = 3

    scenarios = build_scenarios(n_scenarios)
    market_request = MarketRequest(
        market_model_id="bench_noop", rollout_count=rollout_count, horizon_months=horizon_months, seed=seed
    )
    scenario_set = ScenarioSet(
        scenario_set_id="bench_set", title="Augur benchmark set", market_request=market_request, scenarios=scenarios
    )

    # Warmup
    print(
        f"# workload: {n_scenarios} scenarios x {rollout_count} rollouts x {horizon_months} months, seed={seed}",
        flush=True,
    )
    print("# warmup ...", flush=True)
    run_once(scenario_set)

    sim_times: list[float] = []
    mat_times: list[float] = []
    total_times: list[float] = []
    for i in range(repeats):
        sim, mat = run_once(scenario_set)
        sim_times.append(sim)
        mat_times.append(mat)
        total_times.append(sim + mat)
        print(f"# run {i}: simulate={sim:.3f}s materialize={mat:.3f}s total={sim + mat:.3f}s", flush=True)

    print("# === min wall times ===", flush=True)
    print(f"simulate_set: {min(sim_times):.4f}s", flush=True)
    print(f"materialize:  {min(mat_times):.4f}s", flush=True)
    print(f"total:        {min(total_times):.4f}s", flush=True)


if __name__ == "__main__":
    main()
