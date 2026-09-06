"""A partial deduction on an obligation the Rust fixture cannot express.

Everything else about a landlord's rental runs against both engines in
`sim/testing/rental_lifecycle.py`. This one case cannot: `ObligationSpec` takes its
deductible fraction from the gating property's runtime rented share, so an obligation with
no `property_id` and a fraction other than 1.0 has nowhere to put it, and `fixture_encoder`
refuses the scenario rather than encoding it without the fraction. The gap is recorded in
<../rust/docs/parity_gaps.md>; when it closes, this moves up with the rest.
"""

from __future__ import annotations

import pytest
import pytest_bazel

from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    FilingStatus,
    InitialAccountBalance,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    SeriesIndexedAmount,
    TaxProfile,
)
from finance.augur.sim.testing.jax_result import run_jax
from finance.augur.sim.testing.rental_lifecycle import OWNER_AGENT_ID, RENT_SERIES_KEY, TENANT_AGENT_ID, flat_rent_case


def test_obligation_deductible_fraction_scales_deduction() -> None:
    """Partial rental: HOA dues are only deductible up to the rented fraction (0.5
    in this test → only $200 of the $400/mo HOA deducts each month)."""

    end_month = 11
    # Gross rental $30,000/yr (50% rented); HOA $400/mo, 50% deductible → $200/mo × 12 = $2,400.
    # Net ordinary income = $30,000 - $2,400 = $27,600.
    scenario = Scenario(
        agents=[
            Agent(agent_id=OWNER_AGENT_ID),
            Agent(agent_id=TENANT_AGENT_ID),
            Agent(agent_id="hoa"),
            Agent(agent_id="irs"),
        ],
        initial_cash=[
            InitialAccountBalance(agent_id=OWNER_AGENT_ID, account_id="checking", balance=100000),
            InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id="checking", balance=0),
            InitialAccountBalance(agent_id="hoa", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=end_month,
                cause_id="rental_income:p1",
                from_agent_id=TENANT_AGENT_ID,
                from_account_id="checking",
                to_agent_id=OWNER_AGENT_ID,
                to_account_id="checking",
                amount=SeriesIndexedAmount(base_amount=2500, series=RENT_SERIES_KEY, adjustment_period_months=12),
                income_category=ORDINARY_INCOME,
            )
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                end_month=end_month,
                obligation_id="hoa_dues",
                obligation_type="hoa_dues",
                agent_id=OWNER_AGENT_ID,
                from_account_id="checking",
                to_agent_id="hoa",
                to_account_id="checking",
                amount_due=SeriesIndexedAmount(base_amount=400, series=RENT_SERIES_KEY, adjustment_period_months=12),
                deduction_category="ordinary",
                deductible_fraction=0.5,
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id=OWNER_AGENT_ID,
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=12,
    )
    result = run_jax(flat_rent_case(scenario))
    breakdowns = {row["jurisdiction_id"]: row for row in result.events.tax_breakdowns.iter_rows(named=True)}
    assert breakdowns["federal_us"]["ordinary_income_quanta"] / 100 == pytest.approx(27_600, abs=1e-6)


if __name__ == "__main__":
    pytest_bazel.main()
