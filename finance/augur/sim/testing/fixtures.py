"""Scenario pieces the behavioural suites share.

Every case is a `Scenario` and the sampled paths it runs over, which is the one form any
engine consumes — `case.py` says why the authoring happens at that level.

Nothing here knows about an engine, which is why it sits beside `Case` rather than in the
differential harness it grew up in: a suite that states what the answer must be needs these
just as much as one comparing two engines does.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from collections.abc import Sequence
from decimal import Decimal

import numpy as np
from jaxtyping import Float64

from finance.augur.model.series import HomeValueKey, LevelSeriesKey, LocationId, SecurityKey, SecuritySymbol
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    AmountSpec,
    CapitalImprovementEvent,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    ObligationType,
    PropertyLifecycleEvent,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SecurityDistribution,
    SetRentedFractionEvent,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)
from finance.augur.sim.testing.case import Case, flat, levels, scenario

VTI = SecurityKey(symbol=SecuritySymbol("vti"))
BND = SecurityKey(symbol=SecuritySymbol("bnd"))
SF_HOME = HomeValueKey(location_id=LocationId("sf"))

SF = Location(
    location_id="sf",
    display_name="San Francisco",
    jurisdiction_ids=[],
    annual_property_tax_rate=0.0118,
    annual_special_assessment=Decimal(0),
)

type SeriesPaths = dict[LevelSeriesKey, Float64[np.ndarray, " rollout snapshot"]]


def checking(*balances: tuple[str, Decimal]) -> list[InitialAccountBalance]:
    """Opening balances for agents holding one `checking` account each."""

    return [
        InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=balance)
        for agent_id, balance in balances
    ]


def taxed(agent_id: str, *jurisdiction_ids: str, prior_year_tax: Decimal = Decimal(0)) -> TaxProfile:
    """One single filer, paying from `checking` to the `irs` agent.

    The jurisdiction ids are all a profile says about tax law: rates, brackets, deductions
    and exemptions reach both engines through the compiled plan, which resolves them from the
    deployment's own jurisdiction records.
    """

    return TaxProfile(
        agent_id=agent_id,
        jurisdiction_ids=list(jurisdiction_ids),
        tax_authority_agent_id="irs",
        prior_year_tax=prior_year_tax,
    )


def cash_spend(
    obligation_id: str, *, month: int, agent_id: str, to_agent_id: str, amount_due: AmountSpec
) -> ScheduledObligation:
    """A one-off required payment between two `checking` accounts."""

    return ScheduledObligation(
        month=month,
        obligation_id=obligation_id,
        obligation_type=ObligationType.CASH_SPEND,
        agent_id=agent_id,
        from_account_id="checking",
        to_agent_id=to_agent_id,
        to_account_id="checking",
        amount_due=amount_due,
    )


def transfer(
    cause_id: str, *, month: int, from_agent_id: str, to_agent_id: str, amount: AmountSpec
) -> ScheduledTransfer:
    """A one-off untagged movement between two `checking` accounts."""

    return ScheduledTransfer(
        month=month,
        cause_id=cause_id,
        from_agent_id=from_agent_id,
        from_account_id="checking",
        to_agent_id=to_agent_id,
        to_account_id="checking",
        amount=amount,
    )


def shared_case(*, alice_opening: Decimal = Decimal(10)) -> Case:
    """Opening balances, transfers, a FIFO sale, and the events each produces.

    The rollouts follow different price paths and meet at the sale month, so the sale's
    proceeds match while the path to them does not.
    """

    return Case(
        scenario=scenario(
            checking(("alice", alice_opening), ("bob", Decimal(20))),
            horizon_months=3,
            tax_profiles=[],
            scheduled_transfers=[
                transfer("bob_gives_alice_5", month=0, from_agent_id="bob", to_agent_id="alice", amount=Decimal(5))
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=1,
                    end_month=2,
                    cause_id="paycheck",
                    from_agent_id="bob",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=Decimal(1),
                    income_category=ORDINARY_INCOME,
                )
            ],
            scheduled_obligations=[
                cash_spend("required-payment", month=2, agent_id="alice", to_agent_id="bob", amount_due=Decimal("0.50"))
            ],
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    account_id="checking",
                    asset=VTI,
                    purchase_month_index=-12,
                    quantity=2.0,
                    cost_basis_per_unit=Decimal(100),
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=1,
                    cause_id="sell-vti",
                    agent_id="alice",
                    source_account_id="checking",
                    asset=VTI,
                    quantity=1.0,
                    proceeds_account_id="checking",
                )
            ],
        ),
        rollout_count=2,
        series={
            VTI: levels(
                [
                    [Decimal(100), Decimal(150), Decimal(150), Decimal(150)],
                    [Decimal(200), Decimal(150), Decimal(150), Decimal(150)],
                ]
            )
        },
    )


def failure_case() -> Case:
    """A rollout that cannot fund its first obligation and freezes there."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(1)), ("bob", Decimal(0))),
            horizon_months=2,
            tax_profiles=[],
            scheduled_transfers=[
                transfer("must-not-run", month=1, from_agent_id="alice", to_agent_id="bob", amount=Decimal("0.01"))
            ],
            scheduled_obligations=[
                cash_spend("too-large", month=0, agent_id="alice", to_agent_id="bob", amount_due=Decimal("1.01"))
            ],
        ),
        rollout_count=2,
    )


# A year of this salary lands a single filer in the 24% federal and 9.3% California
# brackets, on both jurisdictions' real ladders.
MONTHLY_SALARY = Decimal("16666.67")


def salary(cause_id: str = "alice-paycheck", *, amount: Decimal = MONTHLY_SALARY) -> RecurringTransfer:
    """Twelve months of ordinary income from `payroll` to Alice."""

    return RecurringTransfer(
        start_month=0,
        end_month=11,
        cause_id=cause_id,
        from_agent_id="payroll",
        from_account_id="checking",
        to_agent_id="alice",
        to_account_id="checking",
        amount=amount,
        income_category=ORDINARY_INCOME,
    )


def salary_case(
    *,
    horizon_months: int = 12,
    prior_year_tax: Decimal = Decimal(0),
    recurring_transfers: Sequence[RecurringTransfer] | None = None,
) -> Case:
    """A year of salary assessed federally and by California."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("payroll", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=horizon_months,
            recurring_transfers=[salary()] if recurring_transfers is None else list(recurring_transfers),
            tax_profiles=[taxed("alice", "federal_us", "california", prior_year_tax=prior_year_tax)],
        ),
        rollout_count=1,
    )


ALLOCATION_ACCOUNTS = (("alice", Decimal(12_000)), ("landlord", Decimal(0)), ("irs", Decimal(0)))


def allocation_lots(
    *, bulk_lot_id: str = "a-source-second", older_lot_account_id: str = "brokerage-a"
) -> list[InitialLot]:
    """Two VTI lots in different source accounts plus a BND lot.

    The lot ids sort against source-account order on purpose: which lots a liquidity sale
    reaches has to be decided by the account it draws from, not by lot id.
    """

    return [
        InitialLot(
            lot_id=bulk_lot_id,
            agent_id="alice",
            account_id="brokerage-b",
            asset=VTI,
            purchase_month_index=0,
            quantity=800.0,
            cost_basis_per_unit=Decimal(80),
        ),
        InitialLot(
            lot_id="z-source-first",
            agent_id="alice",
            account_id=older_lot_account_id,
            asset=VTI,
            purchase_month_index=-24,
            quantity=100.0,
            cost_basis_per_unit=Decimal(50),
        ),
        InitialLot(
            lot_id="bond",
            agent_id="alice",
            account_id="brokerage-b",
            asset=BND,
            purchase_month_index=-24,
            quantity=100.0,
            cost_basis_per_unit=Decimal(100),
        ),
    ]


def allocation_policy(
    *,
    source_account_ids: tuple[str, ...] = ("brokerage-a", "brokerage-b"),
    cash_floor: AmountSpec = Decimal(10_000),
    cash_ceiling: AmountSpec = Decimal(30_000),
    purchase_slots_per_sleeve: int = 0,
    rebalance_tolerance: float | None = None,
) -> TargetAllocationPolicy:
    """An equal-weight VTI/BND band on Alice's `checking` account."""

    return TargetAllocationPolicy(
        agent_id="alice",
        account_id="checking",
        source_account_ids=source_account_ids,
        sleeves=[SleeveTarget(asset=VTI, weight=1), SleeveTarget(asset=BND, weight=1)],
        cash_floor=cash_floor,
        cash_ceiling=cash_ceiling,
        purchase_slots_per_sleeve=purchase_slots_per_sleeve,
        rebalance_tolerance=rebalance_tolerance,
    )


def flat_sleeve_prices(*, horizon_months: int) -> SeriesPaths:
    """Both sleeve assets holding at $100 for the whole horizon, in one rollout."""

    return {
        VTI: flat(Decimal(100), rollout_count=1, horizon_months=horizon_months),
        BND: flat(Decimal(100), rollout_count=1, horizon_months=horizon_months),
    }


def allocation_case(
    *,
    horizon_months: int = 12,
    initial_cash: Sequence[InitialAccountBalance] | None = None,
    initial_lots: Sequence[InitialLot] | None = None,
    policy: TargetAllocationPolicy | None = None,
    scheduled_obligations: Sequence[ScheduledObligation] = (),
    recurring_obligations: Sequence[RecurringObligation] = (),
    security_distributions: Sequence[SecurityDistribution] = (),
    tax_profiles: Sequence[TaxProfile] | None = None,
    series: SeriesPaths | None = None,
) -> Case:
    """One target-allocation policy over a VTI/BND sleeve pair, and what it is asked to fund."""

    return Case(
        scenario=scenario(
            list(checking(*ALLOCATION_ACCOUNTS) if initial_cash is None else initial_cash),
            horizon_months=horizon_months,
            initial_lots=allocation_lots() if initial_lots is None else list(initial_lots),
            scheduled_obligations=list(scheduled_obligations),
            recurring_obligations=list(recurring_obligations),
            security_distributions=list(security_distributions),
            tax_profiles=([taxed("alice", "federal_us", "california")] if tax_profiles is None else list(tax_profiles)),
            target_allocation_policies=[allocation_policy() if policy is None else policy],
        ),
        rollout_count=1,
        series=flat_sleeve_prices(horizon_months=horizon_months) if series is None else series,
    )


def target_allocation_case() -> Case:
    """The band raising cash across two source accounts to fund a rent obligation."""

    return allocation_case(
        recurring_obligations=[
            RecurringObligation(
                start_month=1,
                end_month=3,
                obligation_id="rent",
                obligation_type=ObligationType.OUTSIDE_RENT,
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due=Decimal(5_000),
            )
        ]
    )


def target_allocation_purchase_case(*, purchase_slots: int = 1) -> Case:
    """The same band with cash above its ceiling, so the policy buys rather than sells."""

    return allocation_case(
        horizon_months=2,
        initial_cash=checking(("alice", Decimal(100_000)), ("landlord", Decimal(0)), ("irs", Decimal(0))),
        policy=allocation_policy(cash_ceiling=Decimal(20_000), purchase_slots_per_sleeve=purchase_slots),
    )


def home_mortgage(*, annual_interest_rate: float = 0.06, principal: Decimal = Decimal(400_000)) -> MortgageFinancing:
    return MortgageFinancing(
        liability_id="home-mortgage",
        lender_agent_id="bank",
        lender_account_id="checking",
        principal=principal,
        annual_interest_rate=annual_interest_rate,
        term_months=360,
    )


def home_purchase(
    *,
    mortgage: MortgageFinancing | None,
    purchase_price: Decimal = Decimal(500_000),
    down_payment: Decimal = Decimal(100_000),
    buyer_closing_cost: Decimal = Decimal(10_000),
    rented_fraction: float = 0.0,
    land_value_fraction: float = 0.2,
) -> ScheduledPropertyPurchase:
    """Alice buys `home` in San Francisco. `mortgage` is stated because `None` buys it outright."""

    return ScheduledPropertyPurchase(
        month=0,
        cause_id="alice-buys-home",
        property_id="home",
        location_id="sf",
        buyer_agent_id="alice",
        buyer_account_id="checking",
        seller_agent_id="seller",
        seller_account_id="checking",
        purchase_price=purchase_price,
        down_payment=down_payment,
        buyer_closing_cost=buyer_closing_cost,
        rented_fraction=rented_fraction,
        land_value_fraction=land_value_fraction,
        mortgage=mortgage,
    )


def county_property_tax() -> PropertyTaxPolicy:
    """A rate the policy states itself, overriding San Francisco's own ad-valorem rate."""

    return PropertyTaxPolicy(
        property_id="home",
        owner_agent_id="alice",
        from_account_id="checking",
        tax_authority_agent_id="county",
        tax_authority_account_id="checking",
        annual_tax_rate=0.012,
        start_month=0,
    )


FINANCED_PROPERTY_ACCOUNTS = (
    ("alice", Decimal(120_000)),
    ("seller", Decimal(0)),
    ("bank", Decimal(0)),
    ("county", Decimal(0)),
)


def financed_property_case() -> Case:
    """A mortgaged purchase and its first carry month."""

    return Case(
        scenario=scenario(
            checking(*FINANCED_PROPERTY_ACCOUNTS),
            horizon_months=2,
            tax_profiles=[],
            scheduled_property_purchases=[home_purchase(mortgage=home_mortgage())],
            property_tax_policies=[county_property_tax()],
        ),
        rollout_count=1,
        locations={"sf": SF},
    )


PROPERTY_CASHFLOW_ACCOUNTS = (
    ("alice", Decimal(300_000)),
    ("seller", Decimal(0)),
    ("bank", Decimal(0)),
    ("county", Decimal(0)),
    ("tenant", Decimal(60_000)),
    ("manager", Decimal(0)),
    ("irs", Decimal(0)),
)

LEASING_FEE = ScheduledPropertyCashflow(
    month=0,
    property_id="home",
    cause_id="leasing-fee",
    from_agent_id="alice",
    from_account_id="checking",
    to_agent_id="manager",
    to_account_id="checking",
    amount=Decimal(1_000),
    deduction_category="ordinary",
)


def rent_and_management_fee(*, end_month: int) -> list[RecurringPropertyCashflow]:
    return [
        RecurringPropertyCashflow(
            start_month=0,
            end_month=end_month,
            property_id="home",
            cause_id="rent",
            from_agent_id="tenant",
            from_account_id="checking",
            to_agent_id="alice",
            to_account_id="checking",
            amount=Decimal(5_000),
            income_category=ORDINARY_INCOME,
        ),
        RecurringPropertyCashflow(
            start_month=0,
            end_month=end_month,
            property_id="home",
            cause_id="management-fee",
            from_agent_id="alice",
            from_account_id="checking",
            to_agent_id="manager",
            to_account_id="checking",
            amount=Decimal(500),
            deduction_category="ordinary",
        ),
    ]


def property_cashflow_case(
    *,
    purchase: ScheduledPropertyPurchase | None = None,
    property_tax_policies: Sequence[PropertyTaxPolicy] | None = None,
    mortgage_interest_deduction_policies: Sequence[MortgageInterestDeductionPolicy] = (),
) -> Case:
    """A rented home: a one-off leasing fee, a monthly management fee, and monthly rent."""

    return Case(
        scenario=scenario(
            checking(*PROPERTY_CASHFLOW_ACCOUNTS),
            horizon_months=12,
            scheduled_property_purchases=[home_purchase(mortgage=home_mortgage()) if purchase is None else purchase],
            property_tax_policies=(
                [county_property_tax()] if property_tax_policies is None else list(property_tax_policies)
            ),
            scheduled_property_cashflows=[LEASING_FEE],
            recurring_property_cashflows=rent_and_management_fee(end_month=11),
            mortgage_interest_deduction_policies=list(mortgage_interest_deduction_policies),
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
        locations={"sf": SF},
    )


def property_depreciation_case(*, sale: bool) -> Case:
    """A home that becomes half-rented mid-year, takes a capital improvement, and depreciates.

    With `sale`, it is sold in month 12 into a risen market, so the gain carries depreciation
    recapture — which federal caps at the §1250 rate and California runs through its ordinary
    brackets, both read off the same compiled tables.
    """

    horizon_months = 24 if sale else 12
    lifecycle: list[PropertyLifecycleEvent] = [
        SetRentedFractionEvent(month=6, property_id="home", rented_fraction=0.5),
        CapitalImprovementEvent(month=6, property_id="home", amount=Decimal(10_000), description="new roof"),
    ]
    if sale:
        lifecycle.append(PropertySaleEvent(month=12, property_id="home", closing_cost_pct=6.0))
    return Case(
        scenario=scenario(
            checking(*PROPERTY_CASHFLOW_ACCOUNTS),
            horizon_months=horizon_months,
            scheduled_property_purchases=[home_purchase(mortgage=home_mortgage(annual_interest_rate=0.12))],
            scheduled_property_cashflows=[LEASING_FEE],
            recurring_property_cashflows=rent_and_management_fee(end_month=horizon_months - 1),
            property_lifecycle_events=lifecycle,
            mortgage_interest_deduction_policies=[
                MortgageInterestDeductionPolicy(liability_id="home-mortgage", owner_agent_id="alice")
            ],
            tax_profiles=[taxed("alice", "federal_us", "california") if sale else taxed("alice", "federal_us")],
        ),
        rollout_count=1,
        locations={"sf": SF},
        # The home is valued only where it is sold: with no sale nothing reads the level, and
        # the case that does sell needs the market it rose into.
        series={SF_HOME: levels([[Decimal(500_000)] * 12 + [Decimal(750_000)] * 13])} if sale else {},
    )
