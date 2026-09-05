"""The deterministic feature-rich scenario the benchmarks measure and the suites compare on.

Authored as a `Scenario` and its sampled paths, so an engine's own input is the encoding of
the very plan another engine runs — see `sim/testing/case.py` for why that direction is the
only one that keeps a tax rule from reaching one engine and not the other.

Independent agents are combined deliberately: the benchmark exercises the supported policy
surface without one policy family consuming another's liquidity.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from collections.abc import Callable, Sequence
from decimal import Decimal

import numpy as np
from jaxtyping import Float64

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    LocationId,
    PrivateEquityEventKindCode,
    PrivateEquityRegimeCode,
    RentKey,
    SecurityDistributionKey,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    BondHolding,
    CapitalImprovementEvent,
    DistributionTaxSlice,
    FederalSaltCapEntry,
    FederalSaltDeductionPolicy,
    HarvestPolicy,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    ObligationType,
    PrimaryResidenceAssignment,
    PrivateEquityTenderPolicy,
    PropertyLifecycleEvent,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SecurityDistribution,
    SeriesIndexedAmount,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.tlh_harvest import HarvestYieldParams

MIN_FEATURE_HORIZON_MONTHS = 60

ACME = IssuerId("acme")
INFLATION = InflationKey()
SF_RENT = RentKey(location_id=LocationId("sf"))
SF_HOME = HomeValueKey(location_id=LocationId("sf"))
VTI = SecurityKey(symbol=SecuritySymbol("vti"))
BND = SecurityKey(symbol=SecuritySymbol("bnd"))
BND_DISTRIBUTION = SecurityDistributionKey(symbol=SecuritySymbol("bnd"))
SP500 = SecurityKey(symbol=SecuritySymbol("sp500"))

LOCATIONS = {
    "sf": Location(
        location_id="sf",
        display_name="San Francisco",
        jurisdiction_ids=["federal_us", "california"],
        annual_property_tax_rate=0.0118,
        annual_special_assessment=Decimal(250),
    )
}


def _checking(agent_id: str, balance: Decimal) -> InitialAccountBalance:
    return InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=balance)


def _indexed(base_amount: Decimal, series: InflationKey | RentKey, *, months: int = 1) -> SeriesIndexedAmount:
    return SeriesIndexedAmount(base_amount=base_amount, series=series, adjustment_period_months=months)


def _profile(agent_id: str, *jurisdiction_ids: str, prior_year_tax: Decimal = Decimal(0)) -> TaxProfile:
    return TaxProfile(
        agent_id=agent_id,
        jurisdiction_ids=list(jurisdiction_ids),
        tax_authority_agent_id="irs",
        prior_year_tax=prior_year_tax,
    )


def feature_rich_scenario(horizon_months: int, *, extra_obligations: Sequence[ScheduledObligation] = ()) -> Scenario:
    """The whole policy surface in one scenario.

    `extra_obligations` is how a caller probes the scenario without restating it — the
    differential suite injects an unfundable payment to compare the two engines on a frozen
    rollout, which the all-funded scenario cannot show.
    """

    if horizon_months < MIN_FEATURE_HORIZON_MONTHS:
        raise ValueError(f"feature-rich benchmark requires at least {MIN_FEATURE_HORIZON_MONTHS} months")
    last_month = horizon_months - 1
    lifecycle: list[PropertyLifecycleEvent] = [
        SetRentedFractionEvent(month=24, property_id="home", rented_fraction=0.5),
        CapitalImprovementEvent(month=24, property_id="home", amount=Decimal(10_000), description="new roof"),
        PropertySaleEvent(month=48, property_id="home", closing_cost_pct=6.0),
    ]
    return scenario(
        [
            _checking("payroll", Decimal(0)),
            _checking("cashflow", Decimal(50_000)),
            _checking("allocator", Decimal(150_000)),
            _checking("bondholder", Decimal(50_000)),
            _checking("homeowner", Decimal(600_000)),
            _checking("pe_owner", Decimal(1_000)),
            _checking("tlh_owner", Decimal(10_000)),
            InitialAccountBalance(agent_id="tlh_owner", account_id="brokerage", balance=Decimal(0)),
            _checking("landlord", Decimal(0)),
            _checking("vendor", Decimal(0)),
            _checking("seller", Decimal(0)),
            _checking("bank", Decimal(0)),
            _checking("county", Decimal(0)),
            _checking("tenant", Decimal(500_000)),
            _checking("manager", Decimal(0)),
            _checking("irs", Decimal(0)),
        ],
        horizon_months=horizon_months,
        scheduled_transfers=[
            ScheduledTransfer(
                month=2,
                cause_id="indexed-bonus",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="cashflow",
                to_account_id="checking",
                amount=_indexed(Decimal(1_000), INFLATION),
                income_category=ORDINARY_INCOME,
            ),
            ScheduledTransfer(
                month=18,
                cause_id="allocator-windfall",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="allocator",
                to_account_id="checking",
                amount=Decimal(120_000),
                income_category=ORDINARY_INCOME,
            ),
            ScheduledTransfer(
                month=10,
                cause_id="cashflow-charity",
                from_agent_id="cashflow",
                from_account_id="checking",
                to_agent_id="vendor",
                to_account_id="checking",
                amount=Decimal(2_000),
                deduction_category="ordinary",
            ),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=last_month,
                cause_id="cashflow-paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="cashflow",
                to_account_id="checking",
                amount=_indexed(Decimal(8_000), SF_RENT, months=12),
                income_category=ORDINARY_INCOME,
            ),
            RecurringTransfer(
                start_month=0,
                end_month=last_month,
                cause_id="allocator-contribution",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="allocator",
                to_account_id="checking",
                amount=Decimal(5_000),
                income_category=ORDINARY_INCOME,
            ),
            RecurringTransfer(
                start_month=0,
                end_month=last_month,
                cause_id="homeowner-paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="homeowner",
                to_account_id="checking",
                amount=Decimal(15_000),
                income_category=ORDINARY_INCOME,
            ),
        ],
        scheduled_obligations=[
            *extra_obligations,
            ScheduledObligation(
                month=6,
                obligation_id="allocator-large-expense",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id="allocator",
                from_account_id="checking",
                to_agent_id="vendor",
                to_account_id="checking",
                amount_due=Decimal(120_000),
            ),
            ScheduledObligation(
                month=20,
                obligation_id="indexed-repair-bill",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id="cashflow",
                from_account_id="checking",
                to_agent_id="vendor",
                to_account_id="checking",
                amount_due=_indexed(Decimal(2_500), INFLATION),
            ),
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                end_month=last_month,
                obligation_id="living-cost",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id="cashflow",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due=_indexed(Decimal(3_000), SF_RENT, months=12),
            )
        ],
        initial_lots=[
            InitialLot(
                lot_id="allocator-vti-old",
                agent_id="allocator",
                account_id="brokerage-a",
                asset=VTI,
                purchase_month_index=-24,
                quantity=2_000.0,
                cost_basis_per_unit=Decimal(80),
            ),
            InitialLot(
                lot_id="allocator-bnd-old",
                agent_id="allocator",
                account_id="brokerage-b",
                asset=BND,
                purchase_month_index=-18,
                quantity=1_000.0,
                cost_basis_per_unit=Decimal(80),
            ),
            InitialLot(
                lot_id="bondholder-bnd-fund",
                agent_id="bondholder",
                account_id="brokerage",
                asset=BND,
                purchase_month_index=-24,
                quantity=1_000.0,
                cost_basis_per_unit=Decimal(80),
            ),
            InitialLot(
                lot_id="pe-acme-old",
                agent_id="pe_owner",
                account_id="private",
                asset=PrivateEquityAssetKey(issuer_id=ACME),
                purchase_month_index=-36,
                quantity=40.0,
                cost_basis_per_unit=Decimal(10),
            ),
            InitialLot(
                lot_id="pe-acme-new",
                agent_id="pe_owner",
                account_id="private",
                asset=PrivateEquityAssetKey(issuer_id=ACME),
                purchase_month_index=-12,
                quantity=60.0,
                cost_basis_per_unit=Decimal(20),
            ),
            InitialLot(
                lot_id="tlh-sp500",
                agent_id="tlh_owner",
                account_id="brokerage",
                asset=SP500,
                purchase_month_index=0,
                quantity=1_000.0,
                cost_basis_per_unit=Decimal(1),
            ),
        ],
        initial_bonds=[
            BondHolding(
                bond_id="treasury",
                agent_id="bondholder",
                account_id="checking",
                issuer_jurisdiction_id="federal_us",
                face_value=Decimal(100_000),
                purchase_price=Decimal(100_000),
                annual_coupon_rate=0.05,
                purchase_month_index=-1,
                maturity_month_index=35,
            ),
            BondHolding(
                bond_id="california-muni",
                agent_id="bondholder",
                account_id="checking",
                issuer_jurisdiction_id="california",
                face_value=Decimal(100_000),
                purchase_price=Decimal(100_000),
                annual_coupon_rate=0.04,
                purchase_month_index=-1,
                maturity_month_index=47,
            ),
            BondHolding(
                bond_id="corporate",
                agent_id="bondholder",
                account_id="checking",
                face_value=Decimal(100_000),
                purchase_price=Decimal(100_000),
                annual_coupon_rate=0.03,
                purchase_month_index=-1,
                maturity_month_index=last_month,
            ),
            BondHolding(
                bond_id="tips",
                agent_id="bondholder",
                account_id="checking",
                issuer_jurisdiction_id="federal_us",
                face_value=Decimal(100_000),
                purchase_price=Decimal(100_000),
                annual_coupon_rate=0.04,
                inflation_indexed=True,
                purchase_month_index=-1,
                maturity_month_index=last_month,
            ),
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=24,
                cause_id="tlh-half-sale",
                agent_id="tlh_owner",
                source_account_id="brokerage",
                asset=SP500,
                quantity=500.0,
                proceeds_account_id="checking",
            ),
            ScheduledAssetSale(
                month=36,
                cause_id="tlh-final-sale",
                agent_id="tlh_owner",
                source_account_id="brokerage",
                asset=SP500,
                quantity=500.0,
                proceeds_account_id="checking",
            ),
            ScheduledAssetSale(
                month=42,
                cause_id="allocator-explicit-sale",
                agent_id="allocator",
                source_account_id="brokerage-a",
                asset=VTI,
                quantity=100.0,
                proceeds_account_id="checking",
            ),
        ],
        tax_profiles=[
            _profile("bondholder", "federal_us", "california"),
            _profile("homeowner", "federal_us", "california", prior_year_tax=Decimal(10_000)),
            _profile("pe_owner", "federal_us"),
            _profile("tlh_owner", "federal_us"),
        ],
        security_distributions=[
            SecurityDistribution(
                asset=BND,
                agent_id="allocator",
                holding_account_id="brokerage-b",
                to_account_id="checking",
                tax_character=(
                    DistributionTaxSlice(fraction=0.4, issuer_jurisdiction_id="federal_us"),
                    DistributionTaxSlice(fraction=0.6),
                ),
            ),
            SecurityDistribution(
                asset=BND,
                agent_id="bondholder",
                holding_account_id="brokerage",
                to_account_id="checking",
                tax_character=(
                    DistributionTaxSlice(fraction=0.6, issuer_jurisdiction_id="california"),
                    DistributionTaxSlice(fraction=0.4),
                ),
            ),
        ],
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id="allocator",
                account_id="checking",
                source_account_ids=("brokerage-a", "brokerage-b"),
                sleeves=[SleeveTarget(asset=VTI, weight=3), SleeveTarget(asset=BND, weight=2)],
                cash_floor=_indexed(Decimal(20_000), INFLATION),
                cash_ceiling=Decimal(40_000),
                cause_id_prefix="benchmark-allocation",
                purchase_slots_per_sleeve=128,
                rebalance_tolerance=0.1,
            )
        ],
        private_equity_tender_policies=[
            PrivateEquityTenderPolicy(
                owner_agent_id="pe_owner",
                proceeds_account_id="checking",
                liquid_net_worth_floor=_indexed(Decimal(50_000), INFLATION),
            )
        ],
        harvest_policies=[
            HarvestPolicy(
                owner_agent_id="tlh_owner",
                account_id="brokerage",
                asset=SP500,
                yield_params=HarvestYieldParams(
                    peak_annual_yield=0.12,
                    floor_annual_yield=0.004,
                    maturity_decay_exponent=1.5,
                    drawdown_sensitivity=6.0,
                ),
                short_term_fraction=1.0,
            )
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="homeowner-buys-home",
                property_id="home",
                location_id="sf",
                buyer_agent_id="homeowner",
                buyer_account_id="checking",
                seller_agent_id="seller",
                seller_account_id="checking",
                purchase_price=Decimal(500_000),
                down_payment=Decimal(100_000),
                buyer_closing_cost=Decimal(10_000),
                rented_fraction=0.0,
                land_value_fraction=0.2,
                mortgage=MortgageFinancing(
                    liability_id="home-mortgage",
                    lender_agent_id="bank",
                    lender_account_id="checking",
                    principal=Decimal(400_000),
                    annual_interest_rate=0.06,
                    term_months=360,
                ),
            )
        ],
        initial_primary_residences=[PrimaryResidenceAssignment(agent_id="homeowner", property_id="home")],
        primary_residence_events=[SetPrimaryResidenceEvent(month=36, agent_id="homeowner", property_id=None)],
        property_lifecycle_events=lifecycle,
        scheduled_property_cashflows=[
            ScheduledPropertyCashflow(
                month=0,
                property_id="home",
                cause_id="leasing-fee",
                from_agent_id="homeowner",
                from_account_id="checking",
                to_agent_id="manager",
                to_account_id="checking",
                amount=Decimal(1_000),
                deduction_category="ordinary",
            ),
            ScheduledPropertyCashflow(
                month=24,
                property_id="home",
                cause_id="indexed-property-repair",
                from_agent_id="homeowner",
                from_account_id="checking",
                to_agent_id="manager",
                to_account_id="checking",
                amount=_indexed(Decimal(2_500), INFLATION),
                deduction_category="ordinary",
            ),
        ],
        recurring_property_cashflows=[
            RecurringPropertyCashflow(
                start_month=0,
                end_month=47,
                property_id="home",
                cause_id="property-rent",
                from_agent_id="tenant",
                from_account_id="checking",
                to_agent_id="homeowner",
                to_account_id="checking",
                amount=_indexed(Decimal(5_000), SF_RENT, months=12),
                income_category=ORDINARY_INCOME,
            ),
            RecurringPropertyCashflow(
                start_month=0,
                end_month=47,
                property_id="home",
                cause_id="management-fee",
                from_agent_id="homeowner",
                from_account_id="checking",
                to_agent_id="manager",
                to_account_id="checking",
                amount=Decimal(500),
                deduction_category="ordinary",
            ),
        ],
        mortgage_interest_deduction_policies=[
            MortgageInterestDeductionPolicy(
                liability_id="home-mortgage",
                owner_agent_id="homeowner",
                debt_class="acquisition",
                per_jurisdiction_principal_cap={"federal_us": Decimal(750_000), "california": Decimal(1_000_000)},
            )
        ],
        property_tax_policies=[
            PropertyTaxPolicy(
                property_id="home",
                owner_agent_id="homeowner",
                from_account_id="checking",
                tax_authority_agent_id="county",
                tax_authority_account_id="checking",
                annual_tax_rate=0.012,
                start_month=0,
                end_month=48,
            )
        ],
        federal_salt_deduction_policies=[
            FederalSaltDeductionPolicy(
                profile_id="homeowner",
                federal_jurisdiction_id="federal_us",
                cap_schedule=[
                    FederalSaltCapEntry(effective_year_index=0, cap=Decimal(40_000)),
                    FederalSaltCapEntry(effective_year_index=1, cap=Decimal(10_000)),
                ],
            )
        ],
    )


def _matrix(
    value_at: Callable[[int, int], Decimal], *, rollout_count: int, snapshots: int
) -> Float64[np.ndarray, " rollout snapshot"]:
    return np.asarray(
        [[float(value_at(rollout, month)) for month in range(snapshots)] for rollout in range(rollout_count)],
        dtype=np.float64,
    )


def _inflation(_rollout: int, month: int) -> Decimal:
    return Decimal(1) + Decimal("0.02") * min(month // 12, 4) + Decimal("0.001") * (month % 12)


def _rent_level(rollout: int, month: int) -> Decimal:
    return Decimal(1) + Decimal("0.035") * min(month // 12, 4) + Decimal("0.002") * (rollout % 3)


def _vti(_rollout: int, month: int) -> Decimal:
    if month >= 36:
        return Decimal(160)
    if month >= 24:
        return Decimal(90)
    if month >= 12:
        return Decimal(140)
    return Decimal(100)


def _bnd(rollout: int, month: int) -> Decimal:
    return Decimal(80) + Decimal("0.25") * ((rollout + month // 12) % 4)


def _bnd_distribution(rollout: int, month: int) -> Decimal:
    return Decimal("0.20") + Decimal("0.01") * (rollout % 3) + Decimal("0.01") * (month // 24)


def _sp500(_rollout: int, month: int) -> Decimal:
    if month < 6:
        return Decimal(1)
    if month < 18:
        return Decimal("0.80")
    if month < 30:
        return Decimal("0.95")
    if month < 42:
        return Decimal("1.20")
    return Decimal("1.30")


def _home(rollout: int, month: int) -> Decimal:
    if month < 24:
        base = Decimal(500_000)
    elif month < 48:
        base = Decimal(600_000)
    else:
        base = Decimal(720_000)
    return base + Decimal(10_000) * (rollout % 4)


def _pe_regime(rollout: int, month: int) -> int:
    path = rollout % 4
    if path == 1 and month >= 12:
        return PrivateEquityRegimeCode.PUBLIC_MARKET
    if path == 0 and month >= 30:
        return PrivateEquityRegimeCode.ACQUIRED
    if path == 1 and month >= 30:
        return PrivateEquityRegimeCode.COLLAPSED
    return PrivateEquityRegimeCode.PRIVATE_OPERATING


def _pe_event_kind(rollout: int, month: int) -> int:
    kinds = PrivateEquityEventKindCode
    path = rollout % 4
    if path == 0:
        return {6: kinds.TENDER, 18: kinds.TENDER, 30: kinds.ACQUISITION_CASHOUT}.get(month, kinds.NONE)
    if path == 1:
        return {12: kinds.PUBLIC_MARKET_OPEN, 30: kinds.COLLAPSE}.get(month, kinds.NONE)
    if path == 2:
        return {18: kinds.LEGAL_IMPAIRMENT, 24: kinds.ADMIN_MARK_UPDATE}.get(month, kinds.NONE)
    return kinds.FORCED_RECOVERY if month == 24 else kinds.NONE


def _private_equity(*, rollout_count: int, horizon_months: int) -> PrivateEquityBundle:
    snapshots = horizon_months + 1
    codes = np.asarray(
        [[[_pe_regime(r, m), _pe_event_kind(r, m)] for m in range(snapshots)] for r in range(rollout_count)],
        dtype=np.int64,
    )
    rollouts = np.arange(rollout_count, dtype=np.int64)[:, None]
    months = np.arange(snapshots, dtype=np.int64)[None, :]
    # A voluntary tender opportunity is the TENDER event kind; the bundle rejects any
    # producer that desyncs the two, so it is derived here rather than stated twice.
    tender = codes[:, :, 1] == int(PrivateEquityEventKindCode.TENDER)
    return PrivateEquityBundle.from_issuer_arrays(
        ACME,
        mark_usd_per_unit=np.full((rollout_count, snapshots), 100.0),
        regime_code=codes[:, :, 0],
        event_kind_code=codes[:, :, 1],
        sale_opportunity_active=tender,
        sale_capacity_fraction=np.where(tender & (months == 6), 0.25, 1.0),
        eligible_fraction=np.ones((rollout_count, snapshots)),
        forced_sale_fraction=np.where((rollouts % 4 == 2) & (months == 18), 0.3, 0.0),
        liquidity_blocked=(rollouts % 4 == 0) & (months == 18),
        forced_recovery_cashout_usd=np.where((rollouts % 4 == 3) & (months == 24), 10_000.0, 0.0),
        company_valuation_usd=np.zeros((rollout_count, snapshots)),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )


def feature_rich_case(
    *, rollout_count: int, horizon_months: int, extra_obligations: Sequence[ScheduledObligation] = ()
) -> Case:
    if rollout_count <= 0:
        raise ValueError("rollout_count must be positive")
    snapshots = horizon_months + 1
    paths: dict[LevelSeriesKey, Callable[[int, int], Decimal]] = {
        INFLATION: _inflation,
        SF_RENT: _rent_level,
        VTI: _vti,
        BND: _bnd,
        BND_DISTRIBUTION: _bnd_distribution,
        SP500: _sp500,
        SF_HOME: _home,
    }
    return Case(
        scenario=feature_rich_scenario(horizon_months, extra_obligations=extra_obligations),
        rollout_count=rollout_count,
        series={
            key: _matrix(value_at, rollout_count=rollout_count, snapshots=snapshots) for key, value_at in paths.items()
        },
        private_equity=_private_equity(rollout_count=rollout_count, horizon_months=horizon_months),
        locations=LOCATIONS,
    )
