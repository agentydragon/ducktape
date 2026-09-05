"""Random differential cases for the Rust/JAX fuzzer, in two tiers.

A case is a `Scenario` and the sampled paths it runs over (`case.py`), compiled once and run
by both engines. JAX bakes the plan's structure into the compiled program, so what a case
varies decides what it costs. `Shape` is everything that reaches the XLA cache key — which
policy families are present, how many of each, the horizon, the rollout count, and the policy
thresholds and lifecycle months JAX folds in as Python scalars. A value draw is everything the
compiled program takes as a traced input: opening balances, transfer and obligation amounts,
lot bases and quantities, sale months and units, and every external series.

So `build_case(shape, value_seed)` splits its randomness in two. `Draw.structure` is seeded
from the shape and produces identical constants for every case of that shape; `Draw.value` is
seeded from `value_seed` and moves only what the compiled program reads at run time. Many
value seeds over one shape therefore pay one XLA compile between them, and a new shape pays a
fresh one.

Tax law is not drawn at all. A profile names jurisdiction ids and the compiler resolves the
brackets, deduction, §1250 rate and capital-loss cap from the deployment's own records, so
they are constants of the id set — which is what stops a rule reaching one engine only.

Values are biased toward rounding ties (`rounding_boundary.py`): with both engines integer
throughout, a disagreement lives at a rounding site or in an ordering, and a rounding site
only has an opinion where the exact quotient falls on the half. Every draw is made in the
integer units its site divides in — currency quanta, quantity quanta, parts per billion — and
converted to the decimals the scenario states at the point it is authored.

Each family brings its own agents and accounts, so enabling two of them does not make one
consume the other's liquidity and turn every rollout into an early cash failure.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

import numpy as np

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    IndexSeriesKey,
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
from finance.augur.rust.differential.rounding_boundary import half_way_operand
from finance.augur.sim.fixed_point import DEFAULT_UNIT_QUANTA, MONEY_FACTOR_SCALE
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    AmountSpec,
    BondHolding,
    CapitalImprovementEvent,
    Currency,
    DistributionTaxSlice,
    FederalSaltCapEntry,
    FederalSaltDeductionPolicy,
    FixedAmount,
    HarvestPolicy,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    ObligationType,
    OrdinaryIncome,
    PrimaryResidenceAssignment,
    PrivateEquityTenderPolicy,
    PropertyLifecycleEvent,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SecurityDistribution,
    SeriesIndexedAmount,
    SetRentedFractionEvent,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
    TransferDeductionCategory,
)
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.tlh_harvest import HarvestYieldParams

# The currency every case declares, and so the quantum its integer money draws count.
QUANTUM = Currency().quantum
FEDERAL = "federal_us"
STATE = "california"
LOCATION = LocationId("sf")

INFLATION = InflationKey()
RENT = RentKey(location_id=LOCATION)
SF_HOME = HomeValueKey(location_id=LOCATION)
VTI = SecurityKey(symbol=SecuritySymbol("vti"))
BND = SecurityKey(symbol=SecuritySymbol("bnd"))
BND_DISTRIBUTION = SecurityDistributionKey(symbol=SecuritySymbol("bnd"))
SP500 = SecurityKey(symbol=SecuritySymbol("sp500"))
ACME = IssuerId("acme")
ACME_EQUITY = PrivateEquityAssetKey(issuer_id=ACME)

# The level kinds the plan carries as money; the rest are dimensionless indices quantized to
# parts per billion instead. The same split `fixture_encoder` makes, for the same reason.
_MONEY_LEVELS = (SecurityKey, SecurityDistributionKey, HomeValueKey)
_PPB_UNIT = Decimal(1) / MONEY_FACTOR_SCALE


def _money(quanta: int) -> Decimal:
    """`quanta` currency quanta as the exact decimal the compiler converts back to them."""

    return Decimal(quanta) * QUANTUM


def _quantity(quanta: int) -> float:
    """`quanta` quantity quanta as the float `quantity_to_quanta` reads back exactly."""

    return float(Decimal(quanta) / DEFAULT_UNIT_QUANTA)


def _rate(ppb: int) -> float:
    """`ppb` parts per billion as the float rate whose decimal spelling is exactly that.

    Nine places at most, which is what the encoder demands of a bond coupon and a mortgage
    rate: the compiler reads those as an exact rational and Rust as the integer, and they are
    the same number only for a rate that lands on a PPB boundary.
    """

    return float(Decimal(ppb) / MONEY_FACTOR_SCALE)


def _level(key: LevelSeriesKey, value: int) -> Decimal:
    """One stored level as the scenario states it: money in currency units, an index bare."""

    return Decimal(value) * (QUANTUM if isinstance(key, _MONEY_LEVELS) else _PPB_UNIT)


class Family(StrEnum):
    """A policy family a shape may switch on. Each brings its own agents."""

    TRANSFERS = "transfers"
    OBLIGATIONS = "obligations"
    SECURITIES = "securities"
    BONDS = "bonds"
    DISTRIBUTIONS = "distributions"
    TARGET_ALLOCATION = "target_allocation"
    HARVEST = "harvest"
    PRIVATE_EQUITY = "private_equity"
    PROPERTY = "property"
    TAX = "tax"


@dataclass(frozen=True)
class Shape:
    """The structural knobs. Two cases sharing a `Shape` share one compiled program."""

    name: str
    families: frozenset[Family]
    horizon_months: int
    rollout_count: int
    seed: int
    lots: int = 2
    sales: int = 1
    transfers: int = 2
    obligations: int = 1
    bonds: int = 2

    def __post_init__(self) -> None:
        if self.horizon_months < 1:
            raise ValueError(f"horizon must be positive; got {self.horizon_months=}")
        if self.rollout_count < 1:
            raise ValueError(f"rollout count must be positive; got {self.rollout_count=}")
        if Family.SECURITIES in self.families and self.sales > min(self.lots, self.horizon_months):
            raise ValueError(f"one sale per lot and per month at most; got {self.sales=} {self.lots=}")


@dataclass(frozen=True)
class Draw:
    """The two random sources, kept apart so the compile tier is visible at every call site."""

    structure: random.Random
    value: random.Random

    def money(self, low: int, high: int) -> int:
        """A cash amount in currency quanta, odd more often than not.

        Odd matters because the per-unit money sites divide by the quantity scale after
        multiplying by a half-unit quantity: `odd * (2k+1) * scale/2 % scale` is exactly
        `scale/2`, which is the tie.
        """

        amount = self.value.randint(low, high)
        return amount | 1 if self.value.random() < 0.6 else amount

    def half_units(self, whole_unit_budget: int) -> int:
        """A quantity in quantity quanta: an odd multiple of half a unit, so a divide can tie.

        Never zero and never over budget, so how many sales a shape declares cannot depend on
        the value draw — a sale dropped for want of units would move the sale count, and the
        sale count is in the compile key.
        """

        return self.value.randrange(1, 2 * max(whole_unit_budget, 1), 2) * (DEFAULT_UNIT_QUANTA // 2)

    def tie(self, multiplier: int, denominator: int, *, near: int, minimum: int = 1) -> int:
        """`near`, or the nearest operand that puts `multiplier / denominator` exactly on the tie.

        Half the draws stay off the boundary on purpose: a fuzzer that only ever probes ties
        stops covering the ordinary path, where an ordering difference would show instead.
        """

        if self.value.random() < 0.5:
            return max(near, minimum)
        solved = half_way_operand(multiplier, denominator, near=near, minimum=minimum)
        return max(near, minimum) if solved is None else solved


@dataclass
class _Build:
    """One case under construction: the scenario's parts, and the levels they read.

    The parts are the `Scenario` fields verbatim, so `scenario()` names each one once and a
    family that forgets to hand its entries over is a type error rather than a lost policy.
    """

    shape: Shape
    draw: Draw
    initial_cash: list[InitialAccountBalance] = field(default_factory=list)
    initial_lots: list[InitialLot] = field(default_factory=list)
    initial_bonds: list[BondHolding] = field(default_factory=list)
    security_distributions: list[SecurityDistribution] = field(default_factory=list)
    scheduled_transfers: list[ScheduledTransfer] = field(default_factory=list)
    recurring_transfers: list[RecurringTransfer] = field(default_factory=list)
    recurring_property_cashflows: list[RecurringPropertyCashflow] = field(default_factory=list)
    scheduled_obligations: list[ScheduledObligation] = field(default_factory=list)
    recurring_obligations: list[RecurringObligation] = field(default_factory=list)
    scheduled_asset_sales: list[ScheduledAssetSale] = field(default_factory=list)
    scheduled_property_purchases: list[ScheduledPropertyPurchase] = field(default_factory=list)
    initial_primary_residences: list[PrimaryResidenceAssignment] = field(default_factory=list)
    property_lifecycle_events: list[PropertyLifecycleEvent] = field(default_factory=list)
    property_tax_policies: list[PropertyTaxPolicy] = field(default_factory=list)
    mortgage_interest_deduction_policies: list[MortgageInterestDeductionPolicy] = field(default_factory=list)
    federal_salt_deduction_policies: list[FederalSaltDeductionPolicy] = field(default_factory=list)
    private_equity_tender_policies: list[PrivateEquityTenderPolicy] = field(default_factory=list)
    harvest_policies: list[HarvestPolicy] = field(default_factory=list)
    target_allocation_policies: list[TargetAllocationPolicy] = field(default_factory=list)
    tax_profiles: list[TaxProfile] = field(default_factory=list)
    # One row of integer levels per rollout: currency quanta for a money series, parts per
    # billion for an index one. Integer because those are the units the rounding sites divide
    # in, so a tie is solved for here and `_level` states it back as the scenario's decimal.
    series: dict[LevelSeriesKey, list[list[int]]] = field(default_factory=dict)
    private_equity: PrivateEquityBundle = field(default_factory=PrivateEquityBundle.empty)
    locations: dict[str, Location] = field(default_factory=dict)

    @property
    def snapshots(self) -> int:
        return self.shape.horizon_months + 1

    @property
    def agents(self) -> set[str]:
        return {balance.agent_id for balance in self.initial_cash}

    def account(self, agent_id: str, account_id: str, balance_quanta: int) -> None:
        self.initial_cash.append(
            InitialAccountBalance(agent_id=agent_id, account_id=account_id, balance=_money(balance_quanta))
        )

    def month(self) -> int:
        return self.draw.value.randrange(self.shape.horizon_months)

    def path(self, *, low: int, high: int, drift: int = 0) -> list[list[int]]:
        """One positive integer path per rollout, drawn from the value source."""

        return [
            [max(1, self.draw.value.randint(low, high) + drift * month) for month in range(self.snapshots)]
            for _ in range(self.shape.rollout_count)
        ]

    def case(self) -> Case:
        return Case(
            scenario=scenario(
                self.initial_cash,
                horizon_months=self.shape.horizon_months,
                initial_lots=self.initial_lots,
                initial_bonds=self.initial_bonds,
                security_distributions=self.security_distributions,
                scheduled_transfers=self.scheduled_transfers,
                recurring_transfers=self.recurring_transfers,
                recurring_property_cashflows=self.recurring_property_cashflows,
                scheduled_obligations=self.scheduled_obligations,
                recurring_obligations=self.recurring_obligations,
                scheduled_asset_sales=self.scheduled_asset_sales,
                scheduled_property_purchases=self.scheduled_property_purchases,
                initial_primary_residences=self.initial_primary_residences,
                property_lifecycle_events=self.property_lifecycle_events,
                property_tax_policies=self.property_tax_policies,
                mortgage_interest_deduction_policies=self.mortgage_interest_deduction_policies,
                federal_salt_deduction_policies=self.federal_salt_deduction_policies,
                private_equity_tender_policies=self.private_equity_tender_policies,
                harvest_policies=self.harvest_policies,
                target_allocation_policies=self.target_allocation_policies,
                tax_profiles=self.tax_profiles,
            ),
            rollout_count=self.shape.rollout_count,
            series={
                key: levels([[_level(key, value) for value in row] for row in rows])
                for key, rows in self.series.items()
            },
            private_equity=self.private_equity,
            locations=self.locations,
        )


def _income_tag(build: _Build) -> tuple[OrdinaryIncome | None, TransferDeductionCategory | None]:
    """How a transfer is taxed: as income, as a deduction, or as neither."""

    match build.draw.value.randrange(3):
        case 0:
            return ORDINARY_INCOME, None
        case 1:
            return None, "ordinary"
        case _:
            return None, None


def _indexed_amount(build: _Build, key: IndexSeriesKey, *, near: int, month: int | None = None) -> AmountSpec:
    """A fixed amount, or one indexed to `key` with its base solved onto the tie.

    The indexed conversion is `base_amount * level[reset] / level[base]` in parts per billion,
    so the base amount is the free operand: solving it against one rollout's level pair puts
    that month's conversion exactly on the half. `month` is the month the payment falls in
    where the caller knows it, and any month the amount could be read in where it does not.
    """

    if build.draw.value.random() < 0.5:
        return _money(build.draw.money(near // 2, near))
    ppb = build.series[key][0]
    reset = build.draw.value.randrange(1, len(ppb)) if month is None else month
    return SeriesIndexedAmount(base_amount=_money(build.draw.tie(ppb[reset], ppb[0], near=near)), series=key)


def _index_series(build: _Build) -> None:
    """The inflation and rent levels every indexed amount reads.

    Month 0 is the base every conversion divides by, and it is pinned to an even level so a
    half of it exists at all: `base_amount * level / base` can only tie on an even base.
    """

    for key in (INFLATION, RENT):
        rows = build.path(low=800_000_000, high=1_200_000_000, drift=4_000_000)
        for row in rows:
            row[0] = MONEY_FACTOR_SCALE
        build.series[key] = rows


def _transfers(build: _Build) -> None:
    build.account("earner", "checking", build.draw.money(0, 5_000_000))
    build.recurring_transfers.append(
        RecurringTransfer(
            start_month=0,
            end_month=build.shape.horizon_months - 1,
            cause_id="salary",
            from_agent_id="payroll",
            from_account_id="checking",
            to_agent_id="earner",
            to_account_id="checking",
            amount=_indexed_amount(build, INFLATION, near=400_001),
            income_category=ORDINARY_INCOME,
        )
    )
    for index in range(build.shape.transfers):
        outbound = build.draw.value.random() < 0.5
        month = build.month()
        income_category, deduction_category = _income_tag(build)
        build.scheduled_transfers.append(
            ScheduledTransfer(
                month=month,
                cause_id=f"transfer-{index}",
                from_agent_id="earner" if outbound else "payroll",
                from_account_id="checking",
                to_agent_id="vendor" if outbound else "earner",
                to_account_id="checking",
                amount=_indexed_amount(build, RENT, near=250_001, month=month),
                income_category=income_category,
                deduction_category=deduction_category,
            )
        )


def _obligations(build: _Build) -> None:
    # Deliberately thin: an obligation the agent cannot fund freezes the rollout, and both
    # engines have to agree on the month it froze, which is a channel of its own.
    build.account("payer", "checking", build.draw.money(0, 3_000_000))
    for index in range(build.shape.obligations):
        start = build.month()
        build.recurring_obligations.append(
            RecurringObligation(
                start_month=start,
                end_month=build.draw.value.randrange(start, build.shape.horizon_months),
                obligation_id=f"recurring-{index}",
                obligation_type=ObligationType.OUTSIDE_RENT if index % 2 else ObligationType.CASH_SPEND,
                agent_id="payer",
                from_account_id="checking",
                to_agent_id="vendor",
                to_account_id="checking",
                amount_due=_indexed_amount(build, INFLATION, near=200_001),
            )
        )
    build.scheduled_obligations.append(
        ScheduledObligation(
            month=build.month(),
            obligation_id="one-off",
            obligation_type=ObligationType.CASH_SPEND,
            agent_id="payer",
            from_account_id="checking",
            to_agent_id="vendor",
            to_account_id="checking",
            amount_due=_money(build.draw.money(1, 2_000_000)),
        )
    )


def _pool_months(build: _Build, count: int) -> list[int]:
    """Distinct purchase months for one FIFO pool's lots, newest last.

    Distinct because a pool needs a total order: two lots bought in the same month leave FIFO
    with no answer for which sells first, and the scenario refuses them. They are structural,
    since the compiler folds each pool's resolved order into the program.
    """

    return sorted(-month for month in build.draw.structure.sample(range(1, 48), count))


def _lot(
    build: _Build,
    *,
    lot_id: str,
    agent_id: str,
    account_id: str,
    asset: SecurityKey | PrivateEquityAssetKey,
    unit_quanta: int,
    purchase_month: int,
) -> None:
    """One lot whose per-unit basis is odd, and whose total basis is therefore exact.

    Whole units keep `basis_per_unit * units / scale` integral, which the encoder demands; an
    odd per-unit basis is what lets a later part-lot sale of a half-unit multiple land its
    basis allocation exactly on the tie.
    """

    build.initial_lots.append(
        InitialLot(
            lot_id=lot_id,
            agent_id=agent_id,
            account_id=account_id,
            asset=asset,
            purchase_month_index=purchase_month,
            quantity=_quantity(unit_quanta),
            cost_basis_per_unit=_money(build.draw.money(1, 40_000) | 1),
        )
    )


def _securities(build: _Build) -> None:
    build.account("trader", "checking", build.draw.money(100_000, 5_000_000))
    build.account("trader", "brokerage", 0)
    whole_units = [build.draw.value.randrange(2, 40) * DEFAULT_UNIT_QUANTA for _ in range(build.shape.lots)]
    for index, (unit_quanta, month) in enumerate(zip(whole_units, _pool_months(build, len(whole_units)), strict=True)):
        _lot(
            build,
            lot_id=f"trader-vti-{index}",
            agent_id="trader",
            account_id="brokerage",
            asset=VTI,
            unit_quanta=unit_quanta,
            purchase_month=month,
        )
    prices = build.path(low=1, high=60_000)
    remaining = sum(whole_units)
    sale_months = sorted(build.draw.value.sample(range(build.shape.horizon_months), build.shape.sales))
    for index, month in enumerate(sale_months):
        # Each sale spends at most an even share of what is left, so every later sale is still
        # fundable. A sale dropped for want of units would make the sale count — and so the
        # compiled program — a function of the value seed.
        unit_quanta = build.draw.half_units(remaining // (DEFAULT_UNIT_QUANTA * (build.shape.sales - index)))
        remaining -= unit_quanta
        # The proceeds site is `price * units / scale` and units is fixed by now, so price is
        # the free operand. Every rollout prices the sale off its own path, so every rollout
        # gets its own solved tie rather than one shared price.
        for row in prices:
            row[month] = build.draw.tie(unit_quanta, DEFAULT_UNIT_QUANTA, near=row[month])
        build.scheduled_asset_sales.append(
            ScheduledAssetSale(
                month=month,
                cause_id=f"sale-{index}",
                agent_id="trader",
                source_account_id="brokerage",
                asset=VTI,
                quantity=_quantity(unit_quanta),
                proceeds_account_id="checking",
            )
        )
    build.series[VTI] = prices


def _bonds(build: _Build) -> None:
    build.account("bondholder", "checking", build.draw.money(0, 2_000_000))
    for index in range(build.shape.bonds):
        # A multiple of twelve million ppb, so the period rate `rate * period / 12` is exact
        # for every period below and survives the float boundary an indexed coupon crosses.
        period = build.draw.structure.choice([1, 3, 6, 12])
        rate_ppb = build.draw.structure.randrange(1, 60) * 12 * 1_000_000
        # A whole number of coupon periods: a stub period needs a day-count convention the
        # fixture has nowhere to put, and the encoder refuses one.
        periods = build.draw.structure.randrange(1, max(2, (build.shape.horizon_months + 1) // period + 1))
        face = build.draw.tie(rate_ppb * period // 12, MONEY_FACTOR_SCALE, near=build.draw.money(500_000, 40_000_000))
        build.initial_bonds.append(
            BondHolding(
                bond_id=f"bond-{index}",
                agent_id="bondholder",
                account_id="checking",
                issuer_jurisdiction_id=build.draw.structure.choice([FEDERAL, STATE]) if index % 2 else None,
                face_value=_money(face),
                purchase_price=_money(face),
                annual_coupon_rate=_rate(rate_ppb),
                coupon_period_months=period,
                inflation_indexed=build.draw.structure.random() < 0.3,
                purchase_month_index=-1,
                maturity_month_index=periods * period - 1,
            )
        )


def _distributions(build: _Build) -> None:
    build.account("fundholder", "checking", build.draw.money(0, 1_000_000))
    build.account("fundholder", "brokerage", 0)
    _lot(
        build,
        lot_id="fundholder-bnd",
        agent_id="fundholder",
        account_id="brokerage",
        asset=BND,
        unit_quanta=build.draw.value.randrange(2, 30) * DEFAULT_UNIT_QUANTA,
        purchase_month=_pool_months(build, 1)[0],
    )
    build.series.setdefault(BND, build.path(low=1, high=20_000))
    build.series[BND_DISTRIBUTION] = build.path(low=1, high=400)
    # Strictly inside the interval: a slice is a positive fraction, and the two have to
    # allocate the whole payout between them.
    federal_share = build.draw.structure.randrange(1, MONEY_FACTOR_SCALE)
    build.security_distributions.append(
        SecurityDistribution(
            asset=BND,
            agent_id="fundholder",
            holding_account_id="brokerage",
            to_account_id="checking",
            tax_character=(
                DistributionTaxSlice(fraction=_rate(federal_share), issuer_jurisdiction_id=FEDERAL),
                DistributionTaxSlice(fraction=_rate(MONEY_FACTOR_SCALE - federal_share)),
            ),
        )
    )


def _target_allocation(build: _Build) -> None:
    build.account("allocator", "checking", build.draw.money(1_000_000, 30_000_000))
    build.account("allocator", "brokerage-a", 0)
    build.account("allocator", "brokerage-b", 0)
    for asset, account in ((VTI, "brokerage-a"), (BND, "brokerage-b")):
        _lot(
            build,
            lot_id=f"allocator-{asset.symbol}",
            agent_id="allocator",
            account_id=account,
            asset=asset,
            unit_quanta=build.draw.value.randrange(1, 50) * DEFAULT_UNIT_QUANTA,
            purchase_month=_pool_months(build, 1)[0],
        )
        build.series.setdefault(asset, build.path(low=1, high=30_000))
    floor = build.draw.structure.randrange(0, 4_000_000)
    build.target_allocation_policies.append(
        TargetAllocationPolicy(
            agent_id="allocator",
            account_id="checking",
            source_account_ids=("brokerage-a", "brokerage-b"),
            sleeves=[
                SleeveTarget(asset=VTI, weight=build.draw.value.randrange(1, 5)),
                SleeveTarget(asset=BND, weight=build.draw.value.randrange(1, 5)),
            ],
            cash_floor=_money(floor),
            cash_ceiling=_money(floor + build.draw.structure.randrange(1, 4_000_000)),
            cause_id_prefix="fuzz-allocation",
            # One slot per month. JAX preallocates a lot per possible purchase, because each
            # carries its own basis and holding period, and refuses the whole scenario when a
            # sleeve runs out — so a policy that buys most months needs the horizon's worth.
            purchase_slots_per_sleeve=build.shape.horizon_months,
            rebalance_tolerance=_rate(build.draw.structure.randrange(1, 10) * 100_000_000),
        )
    )


def _harvest(build: _Build) -> None:
    build.account("harvester", "checking", build.draw.money(0, 2_000_000))
    build.account("harvester", "brokerage", 0)
    _lot(
        build,
        lot_id="harvester-sp500",
        agent_id="harvester",
        account_id="brokerage",
        asset=SP500,
        unit_quanta=build.draw.value.randrange(1, 40) * DEFAULT_UNIT_QUANTA,
        purchase_month=_pool_months(build, 1)[0],
    )
    build.series[SP500] = build.path(low=1, high=1_000)
    peak = build.draw.structure.randrange(1, 300) * 1_000_000
    build.harvest_policies.append(
        HarvestPolicy(
            owner_agent_id="harvester",
            account_id="brokerage",
            asset=SP500,
            yield_params=HarvestYieldParams(
                peak_annual_yield=_rate(peak),
                floor_annual_yield=_rate(build.draw.structure.randrange(0, peak + 1)),
                # A multiple of a half, which is the only exponent the integer curve evaluates.
                maturity_decay_exponent=_rate(build.draw.structure.randrange(1, 6) * (MONEY_FACTOR_SCALE // 2)),
                drawdown_sensitivity=_rate(build.draw.structure.randrange(0, 12) * MONEY_FACTOR_SCALE),
            ),
            short_term_fraction=_rate(build.draw.structure.randrange(0, MONEY_FACTOR_SCALE + 1)),
        )
    )


def _private_equity_channel(build: _Build, low: int, high: int) -> list[list[int]]:
    return [
        [build.draw.value.randint(low, high) for _ in range(build.snapshots)] for _ in range(build.shape.rollout_count)
    ]


def _private_equity(build: _Build) -> None:
    build.account("pe_owner", "checking", build.draw.money(0, 2_000_000))
    build.account("pe_owner", "private", 0)
    for index, month in enumerate(_pool_months(build, 2)):
        _lot(
            build,
            lot_id=f"pe-acme-{index}",
            agent_id="pe_owner",
            account_id="private",
            asset=ACME_EQUITY,
            unit_quanta=build.draw.value.randrange(1, 60) * DEFAULT_UNIT_QUANTA,
            purchase_month=month,
        )
    marks = _private_equity_channel(build, 1, 30_000)
    regimes = _private_equity_channel(build, 1, max(int(code) for code in PrivateEquityRegimeCode))
    kinds = _private_equity_channel(build, 0, max(int(code) for code in PrivateEquityEventKindCode))
    capacity = _private_equity_channel(build, 0, MONEY_FACTOR_SCALE)
    eligible = _private_equity_channel(build, 0, MONEY_FACTOR_SCALE)
    forced_sale = _private_equity_channel(build, 0, MONEY_FACTOR_SCALE)
    blocked = _private_equity_channel(build, 0, 1)
    recovery = _private_equity_channel(build, 0, 4_000_000)
    # A tender IS the opportunity being taken, so the two channels are one fact told twice and
    # the bundle rejects a producer that desyncs them. It is derived rather than drawn.
    tender = np.asarray(kinds, dtype=np.int64) == int(PrivateEquityEventKindCode.TENDER)
    build.private_equity = PrivateEquityBundle.from_issuer_arrays(
        ACME,
        mark_usd_per_unit=np.asarray([[float(_money(value)) for value in row] for row in marks], dtype=np.float64),
        regime_code=np.asarray(regimes, dtype=np.int64),
        event_kind_code=np.asarray(kinds, dtype=np.int64),
        sale_opportunity_active=tender,
        sale_capacity_fraction=np.asarray([[_rate(value) for value in row] for row in capacity], dtype=np.float64),
        eligible_fraction=np.asarray([[_rate(value) for value in row] for row in eligible], dtype=np.float64),
        forced_sale_fraction=np.asarray([[_rate(value) for value in row] for row in forced_sale], dtype=np.float64),
        liquidity_blocked=np.asarray(blocked, dtype=np.int64) == 1,
        forced_recovery_cashout_usd=np.asarray(
            [[float(_money(value)) for value in row] for row in recovery], dtype=np.float64
        ),
        company_valuation_usd=np.zeros((build.shape.rollout_count, build.snapshots), dtype=np.float64),
        rollout_count=build.shape.rollout_count,
        horizon_months=build.shape.horizon_months,
    )
    build.private_equity_tender_policies.append(
        PrivateEquityTenderPolicy(
            owner_agent_id="pe_owner",
            proceeds_account_id="checking",
            liquid_net_worth_floor=FixedAmount(amount=_money(build.draw.structure.randrange(0, 8_000_000))),
        )
    )


def _property(build: _Build) -> None:
    build.account("homeowner", "checking", build.draw.money(20_000_000, 90_000_000))
    build.account("seller", "checking", 0)
    build.account("bank", "checking", 0)
    build.account("county", "checking", 0)
    build.account("tenant", "checking", 50_000_000)
    build.locations[LOCATION] = Location(
        location_id=LOCATION,
        display_name="Fuzz City",
        jurisdiction_ids=[FEDERAL, STATE],
        annual_property_tax_rate=_rate(build.draw.structure.randrange(1, 20) * 1_000_000),
        annual_special_assessment=_money(build.draw.structure.randrange(0, 50_000)),
    )
    build.series[SF_HOME] = build.path(low=10_000_000, high=90_000_000)
    # Every scalar below is folded into the compiled program, so all of it is structural. The
    # price is a whole currency unit and the land share a whole percent, together keeping
    # `price * (1 - land_share)` — the building basis JAX depreciates — on a whole quantum.
    price = build.draw.structure.randrange(100_000, 800_000) * 100
    principal = build.draw.structure.randrange(0, price)
    purchase_month = build.draw.structure.randrange(0, max(1, build.shape.horizon_months // 2))
    build.scheduled_property_purchases.append(
        ScheduledPropertyPurchase(
            month=purchase_month,
            cause_id="buy-home",
            property_id="home",
            location_id=LOCATION,
            buyer_agent_id="homeowner",
            buyer_account_id="checking",
            seller_agent_id="seller",
            seller_account_id="checking",
            purchase_price=_money(price),
            down_payment=_money(price - principal),
            buyer_closing_cost=_money(build.draw.structure.randrange(0, 2_000_000)),
            rented_fraction=_rate(build.draw.structure.randrange(0, MONEY_FACTOR_SCALE + 1)),
            land_value_fraction=_rate(build.draw.structure.randrange(0, 100) * (MONEY_FACTOR_SCALE // 100)),
            mortgage=(
                MortgageFinancing(
                    liability_id="home-mortgage",
                    lender_agent_id="bank",
                    lender_account_id="checking",
                    principal=_money(principal),
                    annual_interest_rate=_rate(build.draw.structure.randrange(0, 12) * 12_000_000),
                    term_months=build.draw.structure.randrange(12, 361),
                )
                if principal > 0
                else None
            ),
        )
    )
    build.property_tax_policies.append(
        PropertyTaxPolicy(
            property_id="home",
            owner_agent_id="homeowner",
            from_account_id="checking",
            tax_authority_agent_id="county",
            tax_authority_account_id="checking",
            annual_tax_rate=_rate(build.draw.structure.randrange(1, 24) * 1_000_000),
            start_month=purchase_month,
        )
    )
    build.recurring_property_cashflows.append(
        RecurringPropertyCashflow(
            start_month=purchase_month,
            end_month=build.shape.horizon_months - 1,
            property_id="home",
            cause_id="rent",
            from_agent_id="tenant",
            from_account_id="checking",
            to_agent_id="homeowner",
            to_account_id="checking",
            amount=_indexed_amount(build, RENT, near=500_001),
            income_category=ORDINARY_INCOME,
        )
    )
    if purchase_month + 1 < build.shape.horizon_months:
        rest = range(purchase_month + 1, build.shape.horizon_months)
        build.property_lifecycle_events.append(
            SetRentedFractionEvent(
                month=build.draw.structure.choice(rest),
                property_id="home",
                rented_fraction=_rate(build.draw.structure.randrange(0, MONEY_FACTOR_SCALE + 1)),
            )
        )
        build.property_lifecycle_events.append(
            CapitalImprovementEvent(
                month=build.draw.structure.choice(rest),
                property_id="home",
                amount=_money(build.draw.structure.randrange(1, 3_000_000)),
                description="improvement",
            )
        )
        if build.draw.structure.random() < 0.6:
            build.property_lifecycle_events.append(
                PropertySaleEvent(
                    month=build.draw.structure.choice(rest),
                    property_id="home",
                    # Whole basis points, which is the only closing cost the two engines
                    # retain the same fraction of.
                    closing_cost_pct=float(Decimal(build.draw.structure.randrange(0, 1_000)) / 100),
                )
            )
    if principal > 0:
        build.mortgage_interest_deduction_policies.append(
            MortgageInterestDeductionPolicy(
                liability_id="home-mortgage", owner_agent_id="homeowner", debt_class="acquisition"
            )
        )
    # Only when the home is already owned at month 0: an initial assignment names a property
    # the agent has yet to buy otherwise, which neither engine accepts.
    if purchase_month == 0 and build.draw.structure.random() < 0.5:
        build.initial_primary_residences.append(PrimaryResidenceAssignment(agent_id="homeowner", property_id="home"))


# The agents a tax profile may be attached to: every family's own earner, and nobody whose
# only role is to be paid.
_TAXABLE_AGENTS = frozenset({"earner", "trader", "bondholder", "fundholder", "harvester", "homeowner", "payer"})


def _tax(build: _Build) -> None:
    build.account("irs", "checking", 0)
    taxed = sorted(build.agents & _TAXABLE_AGENTS)
    for agent_id in taxed:
        # Which jurisdictions a profile files in is the whole of its tax law: the compiler
        # resolves each one's brackets, deduction and rates from the deployment's records, and
        # the fixture is encoded from those same compiled tables.
        jurisdiction_ids = [FEDERAL, STATE] if build.draw.structure.random() < 0.5 else [FEDERAL]
        # Whether a profile owes estimated tax is structural: it decides whether the compiler
        # emits the quarterly obligation slots at all. How much is traced, and its tie is any
        # prior-year tax congruent to 2 mod 4, the quarter being `prior_year_tax / 4`.
        build.tax_profiles.append(
            TaxProfile(
                agent_id=agent_id,
                jurisdiction_ids=jurisdiction_ids,
                tax_authority_agent_id="irs",
                prior_year_tax=_money(
                    build.draw.tie(1, 4, near=build.draw.money(1, 4_000_000))
                    if build.draw.structure.random() < 0.7
                    else 0
                ),
            )
        )
    if taxed and build.draw.structure.random() < 0.5:
        build.federal_salt_deduction_policies.append(
            FederalSaltDeductionPolicy(
                profile_id=taxed[0],
                federal_jurisdiction_id=FEDERAL,
                cap_schedule=[
                    FederalSaltCapEntry(
                        effective_year_index=year, cap=_money(build.draw.structure.randrange(0, 5_000_000))
                    )
                    for year in range(build.shape.horizon_months // 12 + 1)
                ],
            )
        )


# Order matters: the index series exist before any amount indexes them, and the tax family
# reads the agent set the earlier families built.
_FAMILY_BUILDERS = (
    (Family.TRANSFERS, _transfers),
    (Family.OBLIGATIONS, _obligations),
    (Family.SECURITIES, _securities),
    (Family.BONDS, _bonds),
    (Family.DISTRIBUTIONS, _distributions),
    (Family.TARGET_ALLOCATION, _target_allocation),
    (Family.HARVEST, _harvest),
    (Family.PRIVATE_EQUITY, _private_equity),
    (Family.PROPERTY, _property),
    (Family.TAX, _tax),
)


def build_case(shape: Shape, value_seed: int) -> Case:
    """One case: `shape` fixes the compiled program, `value_seed` moves its traced inputs."""

    build = _Build(shape=shape, draw=Draw(structure=random.Random(shape.seed), value=random.Random(value_seed)))
    build.account("payroll", "checking", 0)
    build.account("vendor", "checking", 0)
    _index_series(build)
    for family, builder in _FAMILY_BUILDERS:
        if family in shape.families:
            builder(build)
    return build.case()


# The families a structural draw may combine. Property and private equity carry the most
# folded-in structure, so they are drawn less often than the cheap families.
_STRUCTURAL_WEIGHTS: tuple[tuple[Family, float], ...] = (
    (Family.TRANSFERS, 0.8),
    (Family.OBLIGATIONS, 0.7),
    (Family.SECURITIES, 0.8),
    (Family.BONDS, 0.5),
    (Family.DISTRIBUTIONS, 0.4),
    (Family.TARGET_ALLOCATION, 0.4),
    (Family.HARVEST, 0.3),
    (Family.PRIVATE_EQUITY, 0.3),
    (Family.PROPERTY, 0.35),
    (Family.TAX, 0.6),
)


# The shapes the value tier holds fixed: one per cluster of policy families, so the set
# covers the surface without the combinatorial compile bill of covering it pair by pair.
VALUE_TIER_SHAPES = (
    Shape(
        name="securities",
        families=frozenset({Family.SECURITIES, Family.TAX}),
        horizon_months=14,
        rollout_count=2,
        seed=1,
        lots=3,
        sales=3,
    ),
    Shape(
        name="cashflow",
        families=frozenset({Family.TRANSFERS, Family.OBLIGATIONS, Family.TAX}),
        horizon_months=14,
        rollout_count=2,
        seed=2,
        transfers=3,
        obligations=2,
    ),
    Shape(
        name="markets",
        families=frozenset({Family.BONDS, Family.DISTRIBUTIONS, Family.TARGET_ALLOCATION, Family.HARVEST}),
        horizon_months=13,
        rollout_count=2,
        seed=3,
        bonds=3,
    ),
    Shape(
        name="property",
        families=frozenset({Family.PROPERTY, Family.TRANSFERS, Family.TAX}),
        horizon_months=25,
        rollout_count=2,
        seed=4,
    ),
)


def random_shape(seed: int) -> Shape:
    """A structural draw. Each one costs a fresh XLA compile, so callers run few of them."""

    rng = random.Random(seed)
    families = {family for family, weight in _STRUCTURAL_WEIGHTS if rng.random() < weight}
    # A shape with nothing in it still runs, but it says nothing; give it the cheapest family.
    families.add(Family.TRANSFERS)
    lots = rng.randrange(1, 4)
    return Shape(
        name=f"random-{seed}",
        families=frozenset(families),
        horizon_months=rng.choice([3, 6, 13, 25]),
        rollout_count=rng.randrange(1, 4),
        seed=seed,
        lots=lots,
        sales=rng.randrange(0, lots + 1),
        transfers=rng.randrange(0, 3),
        obligations=rng.randrange(0, 3),
        bonds=rng.randrange(1, 4),
    )
