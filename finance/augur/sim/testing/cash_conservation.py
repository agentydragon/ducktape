"""Money leaves the modeled world only when something recorded says it did.

Sum every modeled agent's cash and a transfer between two of them cancels, so the total moves
only by what crossed the boundary — a wage from an employer nobody models, a sale to a market
nobody models. The rule below is that this move equals what the engine recorded as crossing.

Disposals are why it is worth saying. When a sale credits proceeds with no matching debit, net
worth stays correct — the lot leaves as the cash arrives — so every agent-facing number looks
right while cash is minted from nothing. Each way of turning something into cash therefore
gets its own case: a scheduled asset sale, a target-allocation sale, a private-equity tender,
and a property sale. Each asserts the disposal actually fired, because a sale that never
happened moves nothing and proves nothing.

Every case is minimal on purpose: in the month under test, the disposal is the only thing
crossing the boundary, so the total's move is the whole story. The mortgage payment and the
tax settlement that share the property sale's month are between modeled agents and cancel.

The stronger form of this — that no flow to an unmodeled counterparty can vanish, in any
month — is not stateable over these channels, because the external boundary is not one of
them. The engine's own counterpart is the double-entry journal it validates on every entry.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    IssuerId,
    LocationId,
    PrivateEquityEventKindCode,
    PrivateEquityRegimeCode,
)
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    CapitalImprovementEvent,
    FixedAmount,
    InitialLot,
    MortgageFinancing,
    PrivateEquityTenderPolicy,
    PropertySaleEvent,
    RecurringObligation,
    ScheduledAssetSale,
    ScheduledPropertyPurchase,
    SleeveTarget,
    TargetAllocationPolicy,
)
from finance.augur.sim.testing.case import Case, flat, levels, scenario
from finance.augur.sim.testing.fixtures import VTI, checking, taxed
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

ACME = PrivateEquityAssetKey(issuer_id=IssuerId("acme"))
QUANTA_PER_UNIT = 100

SALE_MONTH = 4
SALE_UNITS, SALE_PRICE = 5_000.0, Decimal(150)
SALE_PROCEEDS_QUANTA = int(SALE_UNITS * int(SALE_PRICE)) * QUANTA_PER_UNIT

RENT_MONTH = 0
RENT = Decimal(5_000)

TENDER_MONTH, TENDER_HORIZON = 12, 24
TENDER_UNITS, TENDER_MARK = 100.0, 50.0
TENDER_PROCEEDS_QUANTA = int(TENDER_UNITS * TENDER_MARK) * QUANTA_PER_UNIT

PROPERTY_HORIZON, PROPERTY_SALE_MONTH, CAPEX_MONTH = 36, 24, 12
PROPERTY_LOCATION_ID = "loc"
PROPERTY_LOCATIONS = {
    PROPERTY_LOCATION_ID: Location(
        location_id=PROPERTY_LOCATION_ID,
        display_name="Loc",
        jurisdiction_ids=["federal_us"],
        annual_property_tax_rate=0.0,
    )
}


def scheduled_sale_case() -> Case:
    """The reported symptom, minimized: hold $500,000 of an asset, sell it for $750,000.

    Net worth is right either way — the lot leaves as the cash arrives — so the $250,000 gain
    is not what is under test. Crediting $750,000 with nobody debited is.
    """

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(1_000_000))),
            initial_lots=[
                InitialLot(
                    lot_id="bought",
                    agent_id="alice",
                    asset=VTI,
                    quantity=SALE_UNITS,
                    cost_basis_per_unit=Decimal(100),
                    purchase_month_index=0,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=SALE_MONTH,
                    cause_id="sell-vti",
                    agent_id="alice",
                    asset=VTI,
                    quantity=SALE_UNITS,
                    proceeds_account_id="checking",
                )
            ],
            tax_profiles=[],
            horizon_months=6,
        ),
        rollout_count=1,
        series={VTI: flat(SALE_PRICE, rollout_count=1, horizon_months=6)},
    )


def target_allocation_sale_case() -> Case:
    """Alice cannot cover the rent from cash, so the policy raises it by selling VTI."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(1_000)), ("landlord", Decimal(0))),
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    asset=VTI,
                    purchase_month_index=-1,
                    quantity=200.0,
                    cost_basis_per_unit=Decimal(50),
                )
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=RENT_MONTH,
                    obligation_id="alice-rent",
                    obligation_type="rent",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="checking",
                    amount_due=FixedAmount(amount=RENT),
                )
            ],
            target_allocation_policies=[
                TargetAllocationPolicy(
                    agent_id="alice", account_id="checking", sleeves=[SleeveTarget(asset=VTI, weight=1)], cash_ceiling=0
                )
            ],
            tax_profiles=[],
            horizon_months=3,
        ),
        rollout_count=1,
        series={VTI: flat(Decimal(100), rollout_count=1, horizon_months=3)},
    )


def private_equity_tender_case() -> Case:
    """Alice holds illiquid PE and sits under her floor, so the tender sells the whole stake."""

    shape = (1, TENDER_HORIZON + 1)
    opportunity = np.zeros(shape, dtype=np.bool_)
    opportunity[:, TENDER_MONTH] = True
    return Case(
        scenario=scenario(
            checking(("alice", Decimal(100_000))),
            initial_lots=[
                InitialLot(
                    lot_id="acme-lot",
                    agent_id="alice",
                    account_id="checking",
                    asset=ACME,
                    purchase_month_index=-36,
                    quantity=TENDER_UNITS,
                    cost_basis_per_unit=Decimal(10),
                )
            ],
            private_equity_tender_policies=[
                PrivateEquityTenderPolicy(
                    owner_agent_id="alice",
                    proceeds_account_id="checking",
                    liquid_net_worth_floor=FixedAmount(amount=Decimal(500_000)),
                )
            ],
            tax_profiles=[],
            horizon_months=TENDER_HORIZON,
        ),
        rollout_count=1,
        private_equity=PrivateEquityBundle.from_issuer_arrays(
            "acme",
            mark_usd_per_unit=np.full(shape, TENDER_MARK, dtype=np.float64),
            regime_code=np.full(shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64),
            event_kind_code=np.where(opportunity, int(PrivateEquityEventKindCode.TENDER), 0).astype(np.int64),
            sale_opportunity_active=opportunity,
            sale_capacity_fraction=np.ones(shape, dtype=np.float64),
            eligible_fraction=np.ones(shape, dtype=np.float64),
            forced_sale_fraction=np.zeros(shape, dtype=np.float64),
            liquidity_blocked=np.zeros(shape, dtype=np.bool_),
            forced_recovery_cashout_usd=np.zeros(shape, dtype=np.float64),
            company_valuation_usd=np.zeros(shape, dtype=np.float64),
            rollout_count=1,
            horizon_months=TENDER_HORIZON,
        ),
    )


def property_sale_case() -> Case:
    """A mortgaged house, a roof paid for out of pocket, and a sale that pays the loan off.

    Both non-cash-neutral halves of the property lifecycle are here: the capital improvement
    (cash out to a contractor nobody models) and the sale (cash in from a buyer nobody models,
    net of a payoff that extinguishes a liability rather than moving cash).
    """

    home_values = [1.0] * PROPERTY_SALE_MONTH + [1.5] * (PROPERTY_HORIZON + 1 - PROPERTY_SALE_MONTH)
    return Case(
        scenario=scenario(
            checking(("alice", Decimal(1_000_000)), ("seller", Decimal(0)), ("bank", Decimal(0)), ("irs", Decimal(0))),
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="buy-house",
                    property_id="house",
                    location_id=PROPERTY_LOCATION_ID,
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=Decimal(500_000),
                    down_payment=Decimal(100_000),
                    mortgage=MortgageFinancing(
                        liability_id="house-mortgage",
                        lender_agent_id="bank",
                        principal=Decimal(400_000),
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                )
            ],
            property_lifecycle_events=[
                CapitalImprovementEvent(
                    month=CAPEX_MONTH, property_id="house", amount=Decimal(30_000), description="roof"
                ),
                PropertySaleEvent(month=PROPERTY_SALE_MONTH, property_id="house", closing_cost_pct=6.0),
            ],
            tax_profiles=[taxed("alice", "federal_us")],
            horizon_months=PROPERTY_HORIZON,
        ),
        rollout_count=1,
        locations=PROPERTY_LOCATIONS,
        series={
            HomeValueKey(location_id=LocationId(PROPERTY_LOCATION_ID)): levels(
                [[Decimal(str(level)) for level in home_values]]
            )
        },
    )


def _crossed_the_boundary(result: SimulationResult, *, month: int) -> int:
    """What the modeled world gained in one month.

    Every modeled agent's cash, summed: money moving between two of them cancels, so what is
    left is what entered from outside. Snapshot `m` is the state entering month `m`, so
    month `m`'s move is the difference between snapshots `m + 1` and `m`.
    """

    def total(snapshot: int) -> int:
        return int(result.cash.filter(pl.col("month_index") == snapshot).get_column("balance_quanta").sum())

    return total(month + 1) - total(month)


def _proceeds(result: SimulationResult, *, month: int) -> int:
    """What the dispositions in one month say they brought in."""

    return int(
        result.events.lot_dispositions.filter(pl.col("month_index") == month).get_column("proceeds_quanta").sum()
    )


class CashConservationAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_a_scheduled_sale_brings_in_exactly_its_proceeds(self, backend: Backend) -> None:
        result = backend(scheduled_sale_case())

        assert _proceeds(result, month=SALE_MONTH) == SALE_PROCEEDS_QUANTA
        assert _crossed_the_boundary(result, month=SALE_MONTH) == SALE_PROCEEDS_QUANTA

    def test_a_target_allocation_sale_brings_in_exactly_its_proceeds(self, backend: Backend) -> None:
        """The rent it was raised for is between two modeled agents, so it cancels and only
        the sale is left."""

        result = backend(target_allocation_sale_case())
        proceeds = _proceeds(result, month=RENT_MONTH)

        assert proceeds > 0
        assert _crossed_the_boundary(result, month=RENT_MONTH) == proceeds

    def test_a_private_equity_tender_brings_in_exactly_its_proceeds(self, backend: Backend) -> None:
        """The whole 100-unit stake tenders at $50: a $5,000 credit with nothing to debit."""

        result = backend(private_equity_tender_case())

        assert _proceeds(result, month=TENDER_MONTH) == TENDER_PROCEEDS_QUANTA
        assert _crossed_the_boundary(result, month=TENDER_MONTH) == TENDER_PROCEEDS_QUANTA

    def test_a_property_sale_brings_in_its_net_and_not_its_gross(self, backend: Backend) -> None:
        """The mortgage payoff never leaves the modeled world — it extinguishes a liability —
        and the closing costs never arrive, so only the owner's net crosses the boundary."""

        result = backend(property_sale_case())
        sale = result.events.property_sale_events.row(0, named=True)

        assert result.events.rollout_failures.is_empty()
        assert sale["mortgage_payoff_quanta"] > 0, "a payoff of nothing would not tell gross from net"
        assert sale["net_cash_to_owner_quanta"] > 0
        assert _crossed_the_boundary(result, month=PROPERTY_SALE_MONTH) == sale["net_cash_to_owner_quanta"]

    def test_a_capital_improvement_takes_cash_out_of_the_modeled_world(self, backend: Backend) -> None:
        """The other direction, and the anti-vacuity check for the case above: a boundary that
        only ever credits would pass the sale assertion while losing every outflow."""

        result = backend(property_sale_case())
        capex = result.events.capital_improvement_events

        assert capex.height == 1
        assert _crossed_the_boundary(result, month=CAPEX_MONTH) == -int(capex.get_column("amount_quanta").sum())
