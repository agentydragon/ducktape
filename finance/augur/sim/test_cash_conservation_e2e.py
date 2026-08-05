"""Cash is conserved. One assertion that catches every leak, anywhere.

This is the payoff of routing unmodeled counterparties to an external account instead of to
`NO_CODE`. Before, a flow to an unknown (agent, account) was scattered into a padding row
the engine then sliced off: the money vanished, nothing failed, and the only way to notice
was to guard each site individually and remember to. A bond paying into a mistyped account
was exactly that bug.

Now every flow debits one real row and credits another, so the sum over ALL cash rows —
the agents' accounts plus the external one — cannot change. Any leak, in any phase, in code
nobody thought to guard, breaks this test.

Disposals are the half that needs saying out loud, because the invariant is the ONLY thing
that sees them go wrong. When a sale credits proceeds with no matching debit, net worth stays
correct — the lot leaves as the cash arrives — so every agent-facing number looks right while
the ledger mints money. So each way of turning something into cash gets its own scenario
below: a scheduled asset sale, a target-allocation sale, a private-equity tender, and a
property sale. Each also asserts the disposal actually fired, since a sale that never happens
conserves cash trivially and proves nothing.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel
from numpy.typing import NDArray

from finance.augur.model.deterministic import Deterministic
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    IssuerId,
    LocationId,
    PrivateEquityEventKindCode,
    PrivateEquityRegimeCode,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import cents_to_usd
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    BondHolding,
    CapitalImprovementEvent,
    FilingStatus,
    FixedAmount,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    PrivateEquityTenderPolicy,
    PropertySaleEvent,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledAssetPurchase,
    ScheduledAssetSale,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate, simulate_with_external_series

_VTI = SecurityKey(symbol=SecuritySymbol("vti"))
_ACME = PrivateEquityAssetKey(issuer_id=IssuerId("acme"))


def _total_cash_by_month(run: SimulationRun) -> NDArray[np.int64]:
    """Sum over every cash row, including the external account.

    Read off the raw buffer rather than `cash_balances`, which deliberately shows only the
    agents' own accounts — the contra row is exactly what makes the total balance.
    """

    state: NDArray[np.int64] = np.asarray(run.buffers.state.cash_state, dtype=np.int64)
    return np.asarray(state.sum(axis=tuple(range(1, state.ndim))), dtype=np.int64)


def _scenario() -> Scenario:
    """Deliberately busy: money entering from outside (wages, a bond coupon), leaving to
    outside (rent to an unmodeled landlord), moving between modeled agents, and a lot bought
    from the market and sold back to it at a profit."""

    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="bob"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=400_000.0),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance_usd=10_000.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_bonds=[
            BondHolding(
                bond_id="rung",
                agent_id="alice",
                account_id="checking",
                issuer_jurisdiction_id="federal_us",
                face_value_usd=500_000.0,
                purchase_price_usd=500_000.0,
                annual_coupon_rate=0.05,
                coupon_period_months=6,
                purchase_month_index=0,
                maturity_month_index=12,
            )
        ],
        scheduled_asset_purchases=[
            ScheduledAssetPurchase(
                month=1,
                cause_id="buy_vti",
                lot_id="bought",
                agent_id="alice",
                asset=_VTI,
                amount_usd=200_000.0,
                price_per_unit_usd=100.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=6,
                cause_id="sell_vti",
                agent_id="alice",
                asset=_VTI,
                quantity=2_000.0,
                proceeds_account_id="checking",
                price_per_unit_usd=150.0,
            )
        ],
        recurring_transfers=[
            # From an agent that does not exist: an employer outside the model.
            RecurringTransfer(
                start_month=0,
                cause_id="salary",
                from_agent_id="megacorp",
                from_account_id="payroll",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=12_000.0,
                income_category=ORDINARY_INCOME,
            ),
            # To an agent that does not exist: a landlord outside the model.
            RecurringTransfer(
                start_month=0,
                cause_id="rent",
                from_agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="deposits",
                amount_usd=4_000.0,
            ),
        ],
        scheduled_transfers=[
            # Between two modeled agents, which must net to zero across the two rows.
            ScheduledTransfer(
                month=3,
                cause_id="gift",
                from_agent_id="alice",
                from_account_id="checking",
                to_agent_id="bob",
                to_account_id="checking",
                amount_usd=25_000.0,
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=15,
    )


def _scheduled_sale_scenario() -> Scenario:
    """The reported symptom, minimized: buy $500,000 of an asset, sell it for $750,000.

    Net worth is right either way — the lot leaves as the cash arrives — so the $250,000 gain
    is not what is under test. The total is: crediting $750,000 with no counterparty debited
    walks total cash from $1,000,000 to $1,750,000.
    """

    return Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000_000.0)],
        scheduled_asset_purchases=[
            ScheduledAssetPurchase(
                month=1,
                cause_id="buy_vti",
                lot_id="bought",
                agent_id="alice",
                asset=_VTI,
                amount_usd=500_000.0,
                price_per_unit_usd=100.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=4,
                cause_id="sell_vti",
                agent_id="alice",
                asset=_VTI,
                quantity=5_000.0,
                proceeds_account_id="checking",
                price_per_unit_usd=150.0,
            )
        ],
        tax_profiles=[],
        horizon_months=6,
    )


def _target_allocation_scenario() -> Scenario:
    """Alice cannot cover the rent from cash, so the policy raises it by selling VTI."""

    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset=_VTI,
                purchase_month_index=-1,
                quantity=200.0,
                cost_basis_per_unit_usd=50.0,
            )
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                obligation_id="alice_rent",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=5_000.0,
            )
        ],
        external_series=SeriesModelBundle.independent(
            asset_prices=AssetPriceGroups(security={SecuritySymbol("vti"): Deterministic(levels=[100.0] * 4)})
        ),
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id="alice",
                account_id="checking",
                sleeves=[SleeveTarget(asset=_VTI, weight=1)],
                cash_ceiling_usd=0.0,
            )
        ],
        tax_profiles=[],
        horizon_months=3,
    )


def _tender_scenario(*, horizon_months: int) -> Scenario:
    """Alice holds illiquid PE and a floor she is under, so the tender sells the whole stake."""

    return Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100_000.0)],
        initial_lots=[
            InitialLot(
                lot_id="acme_lot",
                agent_id="alice",
                account_id="checking",
                asset=_ACME,
                purchase_month_index=-36,
                quantity=100.0,
                cost_basis_per_unit_usd=10.0,
            )
        ],
        private_equity_tender_policies=[
            PrivateEquityTenderPolicy(
                owner_agent_id="alice",
                proceeds_account_id="checking",
                liquid_net_worth_floor=FixedAmount(amount_usd=500_000.0),
            )
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def _tender_series(*, horizon_months: int, tender_month: int) -> ExternalSeriesContext:
    """One issuer, a flat mark, and a single tender opportunity."""

    shape = (1, horizon_months + 1)
    events = np.zeros(shape, dtype=np.bool_)
    events[:, tender_month] = True
    return ExternalSeriesContext(
        private_equity=PrivateEquityBundle.from_issuer_arrays(
            "acme",
            mark_usd_per_unit=np.full(shape, 50.0, dtype=np.float64),
            regime_code=np.full(shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64),
            event_kind_code=np.where(events, int(PrivateEquityEventKindCode.TENDER), 0).astype(np.int64),
            sale_opportunity_active=events,
            sale_capacity_fraction=np.ones(shape, dtype=np.float64),
            eligible_fraction=np.ones(shape, dtype=np.float64),
            forced_sale_fraction=np.zeros(shape, dtype=np.float64),
            liquidity_blocked=np.zeros(shape, dtype=np.bool_),
            forced_recovery_cashout_usd=np.zeros(shape, dtype=np.float64),
            company_valuation_usd=np.zeros(shape, dtype=np.float64),
            rollout_count=1,
            horizon_months=horizon_months,
        )
    )


_PROPERTY_LOCATION_ID = "loc"
_PROPERTY_LOCATIONS = {
    _PROPERTY_LOCATION_ID: Location(
        location_id=_PROPERTY_LOCATION_ID,
        display_name="Loc",
        jurisdiction_ids=["federal_us"],
        annual_property_tax_rate=0.0,
    )
}


def _property_scenario(*, horizon_months: int, sale_month: int, capex_month: int) -> Scenario:
    """A mortgaged house, a roof paid for out of pocket, and a sale that pays the loan off.

    Both non-cash-neutral halves of the property lifecycle are here: the capital improvement
    (cash out to a contractor nobody models) and the sale (cash in from a buyer nobody models,
    net of a payoff to a lender that takes no cash leg of its own).
    """

    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="seller"), Agent(agent_id="bank"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000_000.0),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="bank", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="buy_house",
                property_id="house",
                location_id=_PROPERTY_LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price_usd=500_000.0,
                down_payment_usd=100_000.0,
                mortgage=MortgageFinancing(
                    liability_id="house_mortgage",
                    lender_agent_id="bank",
                    principal_usd=400_000.0,
                    annual_interest_rate=0.06,
                    term_months=360,
                ),
            )
        ],
        property_lifecycle_events=[
            CapitalImprovementEvent(month=capex_month, property_id="house", amount_usd=30_000.0, description="roof"),
            PropertySaleEvent(month=sale_month, property_id="house", closing_cost_pct=6.0),
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=horizon_months,
    )


def _home_value_series(*, horizon_months: int, sale_month: int) -> ExternalSeriesContext:
    """Flat until the sale month, then up 50% — so the sale realizes a gain worth leaking."""

    levels = [1.0] * sale_month + [1.5] * (horizon_months + 1 - sale_month)
    return ExternalSeriesContext.from_level_blocks(
        [(HomeValueKey(location_id=LocationId(_PROPERTY_LOCATION_ID)), np.asarray([levels], dtype=np.float64))],
        rollout_count=1,
        horizon_months=horizon_months,
    )


def test_total_cash_never_changes() -> None:
    totals = _total_cash_by_month(simulate(_scenario(), rollout_count=2, locations={}))

    assert np.all(totals == totals[0])


def test_the_external_account_is_what_makes_it_balance() -> None:
    """Guards against the test passing vacuously. If nothing actually crossed the model
    boundary, conservation would hold trivially and prove nothing — so assert the external
    row really moved, and in the direction a net-inflow scenario implies (it funds more
    wages, coupons and sale proceeds than it receives in rent and purchases, so it goes
    negative).
    """

    run = simulate(_scenario(), rollout_count=1, locations={})
    # cash_state is (H+1, slot, rollout) — the slot axis is 1, not the last.
    external = np.asarray(run.buffers.state.cash_state)[:, run.plan.external_cash_slot, :]

    assert external[0].sum() == 0
    assert external[-1].sum() < 0


def test_agent_facing_cash_excludes_the_external_account() -> None:
    """`cash_balances` is the agents' money. The external row is an accounting device, and
    surfacing it there would put a fictitious agent in every consumer of the frame."""

    run = simulate(_scenario(), rollout_count=1, locations={})

    assert set(run.cash_balances.get_column("agent_id").to_list()) == {"alice", "bob", "irs"}


def test_a_scheduled_sale_does_not_mint_cash() -> None:
    run = simulate(_scheduled_sale_scenario(), rollout_count=1, locations={})
    totals = _total_cash_by_month(run)

    assert run.events_log.lot_dispositions.get_column("proceeds_usd").sum() == 750_000.0
    assert np.all(totals == totals[0])


def test_a_target_allocation_sale_does_not_mint_cash() -> None:
    run = simulate(_target_allocation_scenario(), rollout_count=1, locations={})
    totals = _total_cash_by_month(run)

    assert run.events_log.lot_dispositions.get_column("proceeds_usd").sum() > 0.0
    assert np.all(totals == totals[0])


def test_a_private_equity_tender_does_not_mint_cash() -> None:
    horizon_months = 24
    run = simulate_with_external_series(
        _tender_scenario(horizon_months=horizon_months),
        rollout_count=1,
        external_series=_tender_series(horizon_months=horizon_months, tender_month=12),
        locations={},
    )
    totals = _total_cash_by_month(run)

    # The whole 100-unit stake tenders at $50: a $5,000 credit that nothing used to debit.
    assert run.events_log.lot_dispositions.get_column("proceeds_usd").sum() == 5_000.0
    assert np.all(totals == totals[0])


def test_a_property_sale_does_not_mint_cash() -> None:
    horizon_months, sale_month = 36, 24
    run = simulate_with_external_series(
        _property_scenario(horizon_months=horizon_months, sale_month=sale_month, capex_month=12),
        rollout_count=1,
        external_series=_home_value_series(horizon_months=horizon_months, sale_month=sale_month),
        locations=_PROPERTY_LOCATIONS,
    )
    totals = _total_cash_by_month(run)

    assert run.events_log.rollout_failures.is_empty()
    assert run.events_log.property_sale_events.row(0, named=True)["net_cash_to_owner_usd"] > 0.0
    assert run.events_log.capital_improvement_events.height == 1
    assert np.all(totals == totals[0])


def test_a_property_sale_debits_the_boundary_by_its_net_not_its_gross() -> None:
    """The mortgage payoff never leaves the modeled world — it extinguishes a liability — so
    only the owner's net crosses the boundary. Nothing else in this scenario moves cash across
    it in the sale month, which makes the external row's move that month the whole story."""

    horizon_months, sale_month = 36, 24
    run = simulate_with_external_series(
        _property_scenario(horizon_months=horizon_months, sale_month=sale_month, capex_month=12),
        rollout_count=1,
        external_series=_home_value_series(horizon_months=horizon_months, sale_month=sale_month),
        locations=_PROPERTY_LOCATIONS,
    )
    # Snapshot `m` holds the state before month `m`, so month `sale_month`'s move is the diff
    # between snapshots `sale_month + 1` and `sale_month`.
    external = np.asarray(run.buffers.state.cash_state)[:, run.plan.external_cash_slot, 0]
    moved_usd = cents_to_usd(int(external[sale_month + 1] - external[sale_month]))
    sale = run.events_log.property_sale_events.row(0, named=True)

    assert moved_usd == pytest.approx(-sale["net_cash_to_owner_usd"], abs=0.01)


if __name__ == "__main__":
    pytest_bazel.main()
