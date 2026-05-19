"""The spike-1 bench scenario — a single deliverable that
exercises every layer the spike adds.

Alice, a single-filer SF resident, has:
  - W-2 paychecks totaling $200k/year (recurring transfer with
    `income_category="ordinary"`).
  - Initial holdings in three positions: VTI, QQQ, BTC. Each is
    a pre-horizon lot at a configurable basis.
  - A floor-triggered sale policy: if checking < $5k, top up to
    $5k by liquidating VTI → QQQ → BTC in order at market
    prices.
  - A $5k/month recurring spend obligation (rent).
  - Federal + California tax profile with prior-year-tax
    estimated knob, single filer, standard deduction.

The market bundle samples each asset as a GBM path with its own
seed, so the 1000 rollouts diverge by market path. Horizon = 60
months (5 years).

`build_bench_scenario()` returns a `Scenario` instance with the
defaults above; tune via keyword args.
"""

from __future__ import annotations

from augur.sim.market import GeometricBrownianPath, MarketBundle
from augur.sim.scenario import (
    Agent,
    FloorTriggeredSalePolicy,
    InitialAccountBalance,
    InitialLot,
    RecurringTransfer,
    Scenario,
    TaxProfile,
)


def build_bench_scenario(
    *,
    horizon_months: int = 60,
    annual_wages_usd: float = 200_000.0,
    monthly_spend_usd: float = 5_000.0,
    floor_usd: float = 5_000.0,
    initial_cash_usd: float = 20_000.0,
    prior_year_tax_usd: float = 40_000.0,
) -> Scenario:
    """The benchable scenario, parameterized for sensitivity
    studies. Defaults reflect the spike-1 spec deliverable."""
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="landlord"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=initial_cash_usd),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-36,
                quantity=300.0,
                cost_basis_per_unit_usd=180.0,
            ),
            InitialLot(
                lot_id="alice_qqq",
                agent_id="alice",
                asset_id="qqq",
                purchase_month_index=-24,
                quantity=120.0,
                cost_basis_per_unit_usd=300.0,
            ),
            InitialLot(
                lot_id="alice_btc",
                agent_id="alice",
                asset_id="btc",
                purchase_month_index=-18,
                quantity=2.0,
                cost_basis_per_unit_usd=25_000.0,
            ),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=annual_wages_usd / 12.0,
                income_category="ordinary",
            ),
            RecurringTransfer(
                start_month=0,
                cause_id="alice_rent",
                from_agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_usd=monthly_spend_usd,
            ),
        ],
        market=MarketBundle(
            paths=[
                GeometricBrownianPath(
                    asset_id="vti",
                    initial_price_usd=240.0,
                    monthly_log_return_mu=0.0067,
                    monthly_log_return_sigma=0.04,
                    rng_seed=11,
                ),
                GeometricBrownianPath(
                    asset_id="qqq",
                    initial_price_usd=400.0,
                    monthly_log_return_mu=0.008,
                    monthly_log_return_sigma=0.05,
                    rng_seed=22,
                ),
                GeometricBrownianPath(
                    asset_id="btc",
                    initial_price_usd=60_000.0,
                    monthly_log_return_mu=0.012,
                    monthly_log_return_sigma=0.15,
                    rng_seed=33,
                ),
            ]
        ),
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status="single",
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
                prior_year_tax_usd=prior_year_tax_usd,
            )
        ],
        floor_triggered_sale_policies=[
            FloorTriggeredSalePolicy(
                agent_id="alice",
                account_id="checking",
                floor_usd=floor_usd,
                replenish_buffer_usd=0.0,
                asset_preference_chain=["vti", "qqq", "btc"],
                cause_id_prefix="alice_floor_sale",
            )
        ],
        horizon_months=horizon_months,
    )
