"""Authored declarations: exact-decimal Pydantic models the app and the portfolio config write.

The compiler's per-table pieces (`sim/compiler/`) lower them into the prepared records a
composed world declares.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

from finance.augur.model.asset_key import AssetKey
from finance.augur.model.series import IndexSeriesKey, SecurityKey
from finance.augur.policy.cash_band import validate_band_bounds
from finance.augur.sim.enums import IncomeCategory
from finance.augur.sim.fixed_point import validate_currency_amount, validate_currency_quantum
from finance.augur.sim.ids import AccountId, AgentId, LotId, PortfolioId
from finance.augur.sim.tlh import TlhAssumptions

type CurrencyAmount = Annotated[Decimal, BeforeValidator(validate_currency_amount)]
type NonNegativeCurrencyAmount = Annotated[CurrencyAmount, Field(ge=0)]
type PositiveCurrencyAmount = Annotated[CurrencyAmount, Field(gt=0)]

CHECKING = AccountId("checking")


class FilingStatus(StrEnum):
    """Federal/state filing status. Today only single-filer is wired through the tax + §121
    math; adding a new variant requires touching every place that branches on filing status
    (bracket lookup keys in jurisdiction YAMLs, §121 cap table in `_apply_property_sale`,
    standard-deduction lookup, …). The enum makes this an explicit blocker on every
    callsite rather than a string typo silently falling through to a missing-key error."""

    SINGLE = "single"


class Currency(BaseModel):
    """One scenario's money unit.

    ``quantum`` is deliberately an exact decimal rather than an ISO exponent:
    it describes the smallest monetary amount this scenario represents.  The
    current default preserves USD-cent scenarios while allowing a zero-decimal
    currency, or another deliberately declared quantum, without hard-coding
    USD into the simulation contract.
    """

    code: str = "USD"
    quantum: Decimal = Decimal("0.01")

    @field_validator("code")
    @classmethod
    def _validate_code(cls, code: str) -> str:
        normalized = code.strip().upper()
        if not normalized:
            raise ValueError("currency code must not be empty")
        return normalized

    @field_validator("quantum", mode="before")
    @classmethod
    def _validate_quantum(cls, quantum: object) -> Decimal:
        return validate_currency_quantum(quantum)


class InitialAccountBalance(BaseModel):
    """Starting cash for one (agent, account) pair at month 0."""

    agent_id: AgentId
    account_id: AccountId
    balance: CurrencyAmount


class FixedAmount(BaseModel):
    """A scalar dollar amount that does not vary by rollout or month."""

    kind: Literal["fixed"] = "fixed"
    amount: CurrencyAmount


class SeriesIndexedAmount(BaseModel):
    """A dollar amount pegged to a sampled external level series.

    The amount is `base_amount` at `base_month_index`. For a
    payment due in month `m`, the simulator first snaps to the current
    adjustment period and then scales linearly by the model level ratio:

    `base_amount * series[reset_month] / series[base_month_index]`.

    With `adjustment_period_months=12`, a rent obligation stays flat for
    the first lease year, resets at month 12, stays flat through month 23,
    and so on.

    `series` is a typed `IndexSeriesKey` (inflation or a location's rent) —
    the index whose level path scales the amount. Asset prices, home values,
    and PE marks are never amount indices, so the role type makes
    `series=SecurityKey(symbol=SP500_SYMBOL)` / `series=HomeValueKey(...)` a type error.
    """

    kind: Literal["series_indexed"] = "series_indexed"
    base_amount: CurrencyAmount
    series: IndexSeriesKey
    base_month_index: NonNegativeInt = 0
    adjustment_period_months: PositiveInt = 1


type AmountSchedule = Annotated[FixedAmount | SeriesIndexedAmount, Field(discriminator="kind")]
type AmountSpec = CurrencyAmount | AmountSchedule


class OrdinaryIncome(BaseModel):
    """Wages, rent, and everything else every jurisdiction taxes.

    Frozen because the tag is a value, not a record: the compiler puts these in a set to
    derive the income-bucket axis, so two `OrdinaryIncome()` must be one key.
    """

    model_config = ConfigDict(frozen=True)

    category: Literal[IncomeCategory.ORDINARY] = IncomeCategory.ORDINARY


class InterestIncome(BaseModel):
    """Interest, tagged with WHO ISSUED the debt — never with whether it is "in-state".

    Whether a jurisdiction taxes this dollar is a relation between the issuer and that
    jurisdiction (`Jurisdiction.taxes_interest_from`), so the same California muni coupon is
    exempt for a Californian and taxable for a New Yorker without the instrument changing.
    """

    model_config = ConfigDict(frozen=True)

    category: Literal[IncomeCategory.INTEREST] = IncomeCategory.INTEREST
    issuer_jurisdiction_id: str | None = Field(
        default=None,
        description=(
            "The taxing authority that issued the debt — `federal_us` for a Treasury, "
            "`california` for a CA muni. `None` means a non-governmental issuer (a corporate "
            "bond), which no jurisdiction exempts."
        ),
    )


type TransferIncomeCategory = Annotated[OrdinaryIncome | InterestIncome, Field(discriminator="category")]
ORDINARY_INCOME = OrdinaryIncome()

type TransferDeductionCategory = Literal["ordinary"]


class ScheduledPropertyCashflow(BaseModel):
    """A property-domain cashflow lowered to a transfer event while the property is active.

    The cashflow is tied to the referenced property's ownership lifecycle. It may be configured
    beyond sale; the engine suppresses it once the property is sold.

    `income_category` tags the amount as taxable income for the recipient; `deduction_category`
    tags it as a deductible expense for the payer (a property management or leasing fee). §469
    passive-activity loss limitations are not modeled.
    """

    month: int
    property_id: str
    cause_id: str
    from_agent_id: AgentId
    from_account_id: AccountId
    to_agent_id: AgentId
    to_account_id: AccountId
    amount: AmountSpec
    income_category: TransferIncomeCategory | None = None
    deduction_category: TransferDeductionCategory | None = None


class RecurringPropertyCashflow(BaseModel):
    """A recurring property-domain cashflow lowered to transfer events while active."""

    start_month: int
    end_month: int | None = None
    property_id: str
    cause_id: str
    from_agent_id: AgentId
    from_account_id: AccountId
    to_agent_id: AgentId
    to_account_id: AccountId
    amount: AmountSpec
    income_category: TransferIncomeCategory | None = None
    deduction_category: TransferDeductionCategory | None = None


class ObligationType(StrEnum):
    """Closed set of `obligation_type` values that flow through dense engine event tables.

    Sim and product callers should use these enum members at construction sites and at
    filter sites in decoded `obligation_settlements` / `obligation_failures` frames.
    """

    CASH_SPEND = "cash_spend"
    OUTSIDE_RENT = "outside_rent"
    ESTIMATED_TAX = "estimated_tax"
    TAX_TRUE_UP = "tax_true_up"
    MORTGAGE_PAYMENT = "mortgage_payment"
    PROPERTY_TAX = "property_tax"
    HOA_DUES = "hoa_dues"
    HOMEOWNERS_INSURANCE = "homeowners_insurance"
    PROPERTY_MAINTENANCE = "property_maintenance"


class RecurringObligation(BaseModel):
    """A required due-now payment that repeats in a month window.

    `deduction_category` tags the paid amount as a deductible expense for `agent_id`, scaled by
    `deductible_fraction` (below 1 for partial deductibility, e.g. the rented share of HOA dues
    on a partial rental).
    """

    start_month: int
    end_month: int | None = None
    obligation_id: str
    obligation_type: str
    agent_id: AgentId
    from_account_id: AccountId
    to_agent_id: AgentId
    to_account_id: AccountId
    amount_due: AmountSpec
    deduction_category: TransferDeductionCategory | None = None
    deductible_fraction: float = Field(default=1.0, ge=0.0, le=1.0)
    # When set, ties the obligation to a property; the engine uses
    # `current.property_rented_fraction[r, prop]` at settlement time to override the
    # compile-time `deductible_fraction` so mid-horizon lifecycle events take effect.
    property_id: str | None = None


class BondHolding(BaseModel):
    """A bond held at scenario start, bought at par and held to maturity.

    Like `InitialLot`, this is a position that already exists — it moves no cash when the
    simulation starts, so a scenario buying into a ladder states its initial cash net of
    the purchase. `purchase_month_index` may pre-date the horizon.

    A bond is not a tax lot. Lots are priced off `external_values` and counted in liquid
    net worth; a held-to-maturity bond is neither marked nor liquid, so it gets its own
    table and its exclusion from liquid net worth is structural rather than a rule someone
    has to remember.

    Phase 1 is par-only. The engine has no discount curve, so a bond bought at a discount
    or premium cannot be valued or amortized — `purchase_price` is required, and
    required to equal the face, so that a real holding bought at 98.5 raises instead of
    being silently treated as par.
    """

    model_config = ConfigDict(frozen=True)

    bond_id: str
    agent_id: AgentId
    # No default: which account the coupons land in is a real decision, and a bond pointing
    # at an account that does not exist resolves to no slot at all — the coupon would be
    # scattered into the dump row and vanish silently rather than raise.
    account_id: AccountId
    # The taxing authority that issued the debt — `federal_us` for a Treasury, `california`
    # for a CA muni, `None` for a corporate issuer. Whether any given holder owes tax on the
    # coupon is a relation between this issuer and that holder's jurisdictions, never a
    # property of the bond: "in-state" is holder-relative.
    issuer_jurisdiction_id: str | None = None
    face_value: PositiveCurrencyAmount
    purchase_price: PositiveCurrencyAmount
    annual_coupon_rate: NonNegativeFloat
    coupon_period_months: PositiveInt = 6
    # TIPS. A flag rather than a separate model because the terms are identical — face,
    # coupon rate, period, maturity — and only the PRINCIPAL those terms apply to differs:
    # a TIPS' principal is the face scaled by CPI since purchase, so its coupon and its
    # redemption both ride that index. A second model would duplicate every field to change
    # one derivation.
    #
    # Consequences worth knowing before setting it: an indexed bond is the one bond whose
    # cashflows are NOT fixed by its terms, so it is priced per rollout off the CPI path
    # rather than from a compile-time table. And its accretion is phantom income — federally
    # taxable in the year it accrues with no cash to pay it — which is exactly the effect
    # that decides TIPS against a tax-free municipal coupon.
    inflation_indexed: bool = False
    purchase_month_index: int
    maturity_month_index: int

    @model_validator(mode="after")
    def _reject_non_par_purchase(self) -> BondHolding:
        # Configured money is parsed as exact Decimal values, so par is an exact equality.
        # Currency representability is validated later against the enclosing scenario's quantum.
        if self.purchase_price != self.face_value:
            raise ValueError(
                f"bond {self.bond_id!r} was bought away from par "
                f"({self.purchase_price=} vs {self.face_value=}). Phase 1 supports par "
                "purchases held to maturity only: valuing a discount or premium requires the "
                "purchase yield, which is a discount factor, and phase 1 has no discount curve. "
                "Pricing bonds away from par is phase 2."
            )
        return self

    @model_validator(mode="after")
    def _reject_unaligned_term(self) -> BondHolding:
        term = self.maturity_month_index - self.purchase_month_index
        if term <= 0:
            raise ValueError(
                f"bond {self.bond_id!r} matures at or before purchase "
                f"({self.maturity_month_index=}, {self.purchase_month_index=})"
            )
        if term % self.coupon_period_months:
            raise ValueError(
                f"bond {self.bond_id!r} has a term of {term} months, which is not a whole number "
                f"of {self.coupon_period_months}-month coupon periods. A stub period would need a "
                "day-count convention and an accrued-interest calculation, neither of which phase 1 has."
            )
        return self


class DistributionTaxSlice(BaseModel):
    """What fraction of a distribution carries one issuer's tax character.

    Real funds are mixed and publish the split, so a single tag would be a lie: an aggregate
    bond fund is part Treasury (state-exempt) and part corporate (exempt nowhere), and a
    national muni fund is federally exempt throughout while only its in-state slice is exempt
    at the state level. Splitting the payout into slices reuses the existing per-issuer
    exemption machinery unchanged — this is the muni-bond path applied several times with
    weights, not new tax machinery.
    """

    model_config = ConfigDict(frozen=True)

    fraction: PositiveFloat
    # Same holder-relative meaning as `BondHolding.issuer_jurisdiction_id`: whether this slice
    # is taxable is a relation between the issuer and the holder's jurisdictions.
    issuer_jurisdiction_id: str | None = None


class SecurityDistribution(BaseModel):
    """A security whose units pay out cash each month, and how that payout is taxed.

    The AMOUNT is exogenous and arrives as `SecurityDistributionKey(symbol=…)` in dollars per
    unit — never a rate. A bond fund's payout tracks the fed rate and credit spreads, which is
    the black-box half of augur's job, and the engine's arithmetic is
    `units_held * distribution_per_unit`, the same multiplication it already does for price.

    The tax CHARACTER is not exogenous, which is why it sits here rather than coming out of the
    model: a fund's jurisdiction breakdown follows its mandate, is published annually, and does
    not move with the economy.

    Scoped to one (agent, holding account, asset) pool because that is what determines both the
    units paid on and where the cash lands. Two positions in the same fund in the same account
    are one pool, not two payouts.

    Every slice is `InterestIncome` today, which is right for the bond funds this exists for and
    wrong for an equity fund: `IncomeCategory` has no qualified-dividend rate, so an equity
    distribution routed through here would be overtaxed as ordinary income. Distributions on an
    equity fund need that third category first.
    """

    model_config = ConfigDict(frozen=True)

    asset: AssetKey
    agent_id: AgentId
    # The account whose units this pays on — lots elsewhere are a different pool.
    holding_account_id: AccountId
    # No default, for the same reason `BondHolding.account_id` has none: cash paid to an account
    # that does not exist is scattered into the dump row and vanishes silently.
    to_account_id: AccountId
    tax_character: tuple[DistributionTaxSlice, ...]

    @model_validator(mode="after")
    def _require_fully_allocated_tax_character(self) -> SecurityDistribution:
        if not self.tax_character:
            raise ValueError(f"security distribution on {self.asset.wire_id!r} declares no tax character")
        total = sum(slice_.fraction for slice_ in self.tax_character)
        # Exactly 1, not "at most 1": a short split would silently pay out less than the fund
        # distributes, which reads as a lower yield rather than as the misconfiguration it is.
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"security distribution on {self.asset.wire_id!r} allocates {total} of its payout; "
                "the tax-character fractions must sum to 1"
            )
        return self


class InitialLot(BaseModel):
    """A pre-existing tax lot, with its exact remaining total cost basis.

    The opening book records this holding without a simulated purchase or cash
    outflow. `purchase_month_index` may be negative and determines the holding
    period of later sales. `account_id` identifies the holding account; lots in
    different accounts are not fungible. Basis is a total, not a rounded unit cost.

    `asset` is the typed `AssetKey` discriminated union identifying what
    is held (sp500 / a crypto symbol / a PE issuer). Dispatch sites match
    on it with `isinstance`; the compiler derives the lot's pricing series
    from it via `asset_price_key`.
    """

    lot_id: LotId
    agent_id: AgentId
    account_id: AccountId = CHECKING
    asset: AssetKey
    purchase_month_index: int
    quantity: float
    cost_basis: CurrencyAmount = Field(
        description="Exact remaining total basis of the opening lot, not a per-unit quote."
    )


class SecuritySleeveTarget(BaseModel):
    """One sleeve of a target allocation: a security's lots in the source accounts, and its relative weight.

    Weights are integers and only their RATIOS matter — `(3, 1)` and `(30, 10)` are the same
    policy. A fraction would be derivable from the weights, so storing fractions would store
    a computed quantity and need a float sum-to-one validator to defend it.
    """

    asset: AssetKey
    weight: NonNegativeInt = Field(
        description="Relative target weight; zero keeps the sleeve sellable but receives no deposits."
    )


class ManagedSleeveTarget(BaseModel):
    """One sleeve of a target allocation: a managed TLH portfolio, sized in money, and its relative weight.

    The portfolio is not its index: it has a value but no units or unit price, and lots of the
    index it tracks are a separate sleeve.
    """

    portfolio_id: PortfolioId
    weight: NonNegativeInt = Field(
        description="Relative target weight; zero keeps the sleeve sellable but receives no deposits."
    )


type SleeveTarget = SecuritySleeveTarget | ManagedSleeveTarget


class CashflowOnly(BaseModel):
    """Only cash moving in or out shifts the account toward its target.

    A withdrawal takes more from the overweight sleeves and a deposit puts more into the
    underweight ones, so the portfolio drifts back whenever money moves. A month with no cash
    need trades nothing, however far the sleeves have drifted.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["cashflow_only"] = "cashflow_only"


class DriftBand(BaseModel):
    """Trade on drift alone: sell the overweight sleeves down, buy the underweight ones up.

    Not the cash band. `cash_floor`/`cash_ceiling` say how much cash to hold; this band says how
    far a sleeve may stray from its own target before the policy trades in a month with no cash
    need at all.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["drift_band"] = "drift_band"
    tolerance: NonNegativeFloat = Field(
        description=(
            "Drift, relative to a sleeve's own target, at which the policy trades. 0.25 is the "
            "'25' of the standard 5/25 rule. `0.0` is a real setting, not a disabled one: it "
            "rebalances whenever anything is off by a cent."
        )
    )


type RebalancingRule = Annotated[CashflowOnly | DriftBand, Field(discriminator="kind")]


class TargetAllocationPolicy(BaseModel):
    """Funding policy for one agent cash account: hold cash in a band, sell toward a target.

    Sales move TOWARD a target rather than down an ordered sell list. When
    the account's projected end-of-month balance falls below `cash_floor`, the policy
    raises enough to reach `cash_ceiling`, taking from the most overweight sleeve first
    so what remains is as close to the target ratios as the raise allows.

    The band is (s,S): crossing the floor refills to the ceiling, not back to the floor.
    Refilling to the floor would put the agent back at its trigger next month, making it a
    forced seller into every dip — which is the risk this whole model exists to price.

    **The ceiling carries two meanings, and which apply depends on `allow_purchases`.**
    It is always the refill target a raise aims at. With purchases enabled it is also an
    invest-above-this line: cash projected above the ceiling is invested down to the FLOOR,
    water-filled into whichever sleeves are furthest below target. Otherwise the policy
    never buys and surplus cash simply accumulates.

    `rebalancing` decides whether drift alone can trade. `CashflowOnly` moves toward the target
    only when money is going in or out anyway; `DriftBand` also trades in a month with no cash
    need at all, which costs turnover and realizes gains that were not otherwise due.

    Sleeves the policy does not name are outside the target denominator entirely: never sold
    to fund the band, and not counted when measuring what is overweight. That is what makes
    a target alongside an untradeable holding — private equity before liquidity, a bond that
    will be held to maturity — expressible at all.
    """

    agent_id: AgentId
    # Cash account the band governs: it receives sale proceeds and pays the matching obligations.
    account_id: AccountId
    # Holding accounts the policy may sell from. Empty means the funding account only.
    source_account_ids: tuple[AccountId, ...] = ()
    sleeves: list[SleeveTarget]
    # `AmountSpec = Decimal | AmountSchedule` — an exact decimal for a constant band, or a
    # `SeriesIndexedAmount` (e.g. `series=InflationKey()`) to hold the band in real terms.
    cash_floor: AmountSpec = Decimal(0)
    cash_ceiling: AmountSpec
    cause_id_prefix: str = "allocation_sale"
    rebalancing: RebalancingRule = Field(
        description=(
            "How the account moves toward its target. Required, and deliberately without a "
            "default: `CashflowOnly` and `DriftBand` are both real strategies with different "
            "turnover and tax drag, which is the very difference the allocation study exists to "
            "measure. A default would pick one of them for every caller that did not think "
            "about it, and the pick would not appear at the call site or in any report."
        )
    )
    allow_purchases: bool = Field(
        description=(
            "Whether surplus cash is invested and drift rebalancing may buy underweight sleeves. "
            "False is sales-only: surplus cash accumulates. Required because this changes the "
            "investment strategy. Each settled purchase creates its own lot and cost basis."
        )
    )

    @model_validator(mode="after")
    def _reject_duplicate_and_inverted(self) -> TargetAllocationPolicy:
        if not self.cause_id_prefix.strip():
            raise ValueError("target-allocation cause prefix must not be empty")
        if len(set(self.source_account_ids)) != len(self.source_account_ids):
            raise ValueError("target-allocation source accounts must be unique")
        if not self.sleeves:
            raise ValueError(
                f"target-allocation policy for {self.agent_id}/{self.account_id} names no sleeves; "
                "a policy with an empty target can never raise cash and would fail every obligation "
                "the account cannot already cover"
            )
        if not any(sleeve.weight > 0 for sleeve in self.sleeves):
            raise ValueError("target-allocation policy requires at least one positive sleeve weight")
        named = [
            str(sleeve.asset) if isinstance(sleeve, SecuritySleeveTarget) else f"portfolio {sleeve.portfolio_id}"
            for sleeve in self.sleeves
        ]
        if len(set(named)) != len(named):
            duplicated = sorted({name for name in named if named.count(name) > 1})
            raise ValueError(
                f"target-allocation policy for {self.agent_id}/{self.account_id} names {duplicated} "
                "more than once; a sleeve weighted twice is counted twice and skews every target"
            )
        # Band ordering is checked on the CONFIGURED amounts because per-month values may be
        # CPI-indexed, hence traced, and a traced value cannot drive a raise. Indexing scales
        # both bounds by the same series, so an ordering that holds here holds on every path.
        validate_band_bounds(floor=_base_amount(self.cash_floor), ceiling=_base_amount(self.cash_ceiling))
        if isinstance(self.rebalancing, DriftBand) and not self.allow_purchases:
            raise ValueError(
                f"target-allocation policy for {self.agent_id}/{self.account_id} sets a drift band of "
                f"{self.rebalancing.tolerance} but purchases are disabled. A rebalance sells the overweight "
                "sleeves and buys the underweight ones; with buying disabled it would only ever sell, "
                "draining the portfolio into cash a little more on every trigger"
            )
        return self


def _base_amount(spec: AmountSpec) -> Decimal:
    """The configured base of an amount spec, for compile-time checks that compare two specs."""

    match spec:
        case Decimal():
            return spec
        case FixedAmount():
            return spec.amount
        case SeriesIndexedAmount():
            return spec.base_amount


class TaxProfile(BaseModel):
    """A taxed agent's tax-time configuration. At spike 1 only single filers are modeled;
    later layers add MFJ / HoH and any filing-status-driven branching."""

    agent_id: AgentId
    filing_status: FilingStatus = FilingStatus.SINGLE
    jurisdiction_ids: list[str] = Field(
        description='Ordered list of taxing authorities — typically `["federal_us", "california"]` for a CA resident.'
    )
    tax_authority_agent_id: AgentId = Field(
        description="Destination of tax-payment transfers — a bookkeeping sink, not a taxed agent itself."
    )
    payment_account_id: AccountId = Field(
        default=CHECKING, description="The agent's account the engine debits for estimated-tax and true-up payments."
    )
    tax_authority_account_id: AccountId = Field(
        default=CHECKING, description="The matching credit account on the tax authority's side."
    )
    prior_year_tax: NonNegativeCurrencyAmount = Field(
        default=Decimal(0),
        description=(
            "Aggregate safe-harbor target used to size quarterly estimated payments. If "
            "0, no quarterly estimates are emitted and the January true-up pays the full "
            "accrued tax."
        ),
    )


class MortgageFinancing(BaseModel):
    """Mortgage terms attached to a property purchase."""

    liability_id: str
    lender_agent_id: AgentId
    lender_account_id: AccountId = CHECKING
    principal: CurrencyAmount
    annual_interest_rate: float
    term_months: PositiveInt


class SetRentedFractionEvent(BaseModel):
    """Mid-horizon transition: set a property's rented_fraction to a new value at `month`.

    Subsumes the previously-separate start/stop/change-rental-plan events —
    `rented_fraction=1.0` is "start full rental", `0.0` is "stop renting", anything in
    between is a partial rental (rooms / ADU). Validation ensures values are in [0.0, 1.0].
    """

    kind: Literal["set_rented_fraction"] = "set_rented_fraction"
    month: int
    property_id: str
    rented_fraction: float = Field(ge=0.0, le=1.0)


class PrimaryResidenceAssignment(BaseModel):
    """Initial main-home assignment for one agent.

    Absence means the agent has no primary residence at scenario start. This is agent-scoped
    rather than property-scoped so the schema cannot represent two simultaneous primary
    residences for the same taxpayer.
    """

    agent_id: AgentId
    property_id: str


class SetPrimaryResidenceEvent(BaseModel):
    """Mid-horizon transition: assign or clear an agent's primary residence."""

    kind: Literal["set_primary_residence"] = "set_primary_residence"
    month: int
    agent_id: AgentId
    property_id: str | None


class PropertySaleEvent(BaseModel):
    """Mid-horizon sale of a property.

    At `month`:
    - gross proceeds = `property_market_value_at_month × (1 - closing_cost_pct / 100)` where
      market value is derived from the home_value series for the property's location.
    - any outstanding mortgage on this property is paid off from the proceeds.
    - net cash to owner = gross_proceeds - mortgage_balance.
    - realized gain = gross_proceeds - (purchase_price + capex - cumulative_depreciation).
    - depreciation recapture (§1250 unrecaptured) = min(realized_gain, cumulative_dep).
      Federal taxes this through the lesser-of-marginal-or-25%-cap path; CA-style links
      treat it as ordinary income inside their standard bracket walk.
    - long-term capital gain on the post-recapture, post-§121-exclusion remainder.
    - property is marked sold; rented_fraction → 0; no further depreciation, MID, SALT,
      Schedule E, or rental income for this property.

    §121 primary-residence exclusion uses the owning agent's primary-residence assignment:
    24 qualifying months in the last 60 excludes up to the filing-status cap from
    post-recapture gain.
    """

    kind: Literal["property_sale"] = "property_sale"
    month: int
    property_id: str
    closing_cost_pct: float = Field(ge=0.0, le=100.0)


class CapitalImprovementEvent(BaseModel):
    """Mid-horizon capital improvement (roof, kitchen remodel, HVAC, etc).

    Debits the property owner's cash by `amount` and increases the property's depreciable
    building basis by the same amount. Future depreciation accrues on the new (higher) basis.
    The new improvement is treated as adding to the existing depreciation track rather than
    starting a separate 27.5-year clock (a simplification — cost-segregation studies in
    practice can split improvements into 5/7/15/27.5 year buckets; out of scope here).
    """

    kind: Literal["capital_improvement"] = "capital_improvement"
    month: int
    property_id: str
    amount: PositiveCurrencyAmount
    description: str = ""


type PropertyLifecycleEvent = Annotated[
    SetRentedFractionEvent | CapitalImprovementEvent | PropertySaleEvent, Field(discriminator="kind")
]


class ScheduledPropertyPurchase(BaseModel):
    """Purchase a real property at a fixed month.

    The engine records property state, one owner stake row, optional
    mortgage origination, and a cash transfer for down payment plus
    buyer closing costs. Mortgage proceeds are not routed through the
    buyer's cash account in this first slice; the purchase is booked
    net, with the debt appearing as a liability.

    `rented_fraction` (0..1) is the share of the property that is rented out at purchase
    month. 0.0 = not rented; 1.0 = fully rented; values in between = mixed-use
    (proportional Schedule E + reduced MID/SALT). Primary-residence use is modeled
    separately via agent-level primary-residence assignments and events.
    """

    month: int
    cause_id: str
    property_id: str
    location_id: str
    buyer_agent_id: AgentId
    buyer_account_id: AccountId
    seller_agent_id: AgentId
    seller_account_id: AccountId = CHECKING
    purchase_price: CurrencyAmount
    down_payment: CurrencyAmount
    buyer_closing_cost: NonNegativeCurrencyAmount = Decimal(0)
    mortgage: MortgageFinancing | None = None
    rented_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    # Tax-assessor split between land (non-depreciable) and building (depreciable, 27.5-year
    # straight-line under §168). Default 0.20 (20% land / 80% building) is a common
    # cost-segregation rule of thumb absent assessor data. The engine accrues monthly
    # depreciation = `building_basis × rented_fraction / (27.5 × 12)` where building_basis =
    # `purchase_price × (1 - land_value_fraction) + buyer_closing_cost`.
    land_value_fraction: float = Field(default=0.20, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _reject_unfunded_purchase(self) -> ScheduledPropertyPurchase:
        # The seller is paid the down payment and nothing else, while the buyer books
        # `purchase_price - principal` of equity. Those two agree only when the down payment and
        # the mortgage between them cover the price; short of that the buyer gains equity nobody
        # paid for and the seller is handed less than the property was sold for.
        principal = self.mortgage.principal if self.mortgage is not None else Decimal(0)
        if self.down_payment + principal != self.purchase_price:
            raise ValueError(
                f"property purchase {self.cause_id!r} is not funded: "
                f"{self.down_payment=} + {principal=} != {self.purchase_price=}. "
                "Closing costs are paid on top of the price and are not part of this identity."
            )
        return self


class PropertyTaxPolicy(BaseModel):
    """Monthly property-tax carrying cost for an owned property.

    `annual_tax_rate` can override location reference data; when it
    is `None`, the rate comes from `Location.annual_property_tax_rate`.
    """

    property_id: str
    owner_agent_id: AgentId
    from_account_id: AccountId = CHECKING
    tax_authority_agent_id: AgentId
    tax_authority_account_id: AccountId = CHECKING
    annual_tax_rate: float | None = None
    start_month: int = 0
    end_month: int | None = None


class PrivateEquityTenderPolicy(BaseModel):
    """Sell private-equity units at sampled tender events to lift liquid net worth to a floor.

    At each tender event for any held PE position belonging to `owner_agent_id`, the engine:

    1. Computes the rollout's current liquid net worth (cash + non-PE lots × their current
       sampled price, by definition excluding PE itself — PE is illiquid).
    2. Evaluates `liquid_net_worth_floor` at the event month. `SeriesIndexedAmount` lets the
       floor inflate (or peg to any other series); a `FixedAmount` keeps it nominal.
    3. `shortfall = max(0, floor - lnw)`.
    4. Sells `min(units_held_in_issuer, shortfall / mark)` units of the tendering issuer at
       the issuer's per-rollout mark (from the PE trajectory bundle).
    5. Proceeds credit `proceeds_account_id`; cap-gain flows through the standard FIFO
       lot-drain machinery (LTCG / STCG by holding period — IRS treats crypto/property/PE
       identically for cap-gains purposes).

    Multiple tenders firing in the same month process in deterministic issuer order; each
    sale updates the cash balance + lot remaining before the next tender's LNW check runs,
    so the floor genuinely caps aggregate sale across all same-month tenders.

    Scope notes (deferred enhancements, captured here so a future reader knows what's missing):

    - **Per-issuer policies.** v1 supports one global policy per agent; a per-issuer policy
      list (`floor_by_issuer: dict[str, AmountSchedule]`) could allow "sell only
      issuer A to reach $X, never sell issuer B") if needed.
    - **Partial-tender fraction.** Real tenders sometimes cap participation (e.g. "you may
      sell up to 20% of your holdings"). The trajectory artifact carries a
      `saleable_fraction` field already; consuming it would let the policy gate units sold
      to `min(units_held × saleable_fraction, shortfall / mark)`. Not wired today.
    - **§1202 QSBS exclusion.** Federal exclusion of up to $10M / 10× basis on qualified
      small-business stock held ≥5 years. Not modeled; the user confirmed they don't hold
      QSBS-eligible PE.
    """

    owner_agent_id: AgentId
    proceeds_account_id: AccountId = CHECKING
    liquid_net_worth_floor: AmountSchedule


class TlhCohort(BaseModel):
    """One tax lot of a direct-indexing statement: what it is worth, its basis and when it was bought."""

    model_config = ConfigDict(extra="forbid")

    value: NonNegativeCurrencyAmount = Field(description="Market value at the opening mark, month zero's price.")
    cost_basis: NonNegativeCurrencyAmount = Field(
        description="Remaining adjusted basis, already net of harvesting before the scenario opens."
    )
    purchase_month_index: int


class TlhPortfolioSpec(BaseModel):
    """A separately owned reduced-form portfolio, not an ordinary holding plus a policy.

    `asset` is the index the portfolio tracks; its price series carries the cohorts' value.
    """

    model_config = ConfigDict(extra="forbid")

    portfolio_id: PortfolioId = Field(min_length=1)
    owner_agent_id: AgentId
    account_id: AccountId
    asset: AssetKey
    initial_cohorts: list[TlhCohort]
    assumptions: TlhAssumptions

    @model_validator(mode="after")
    def _validate_index(self) -> TlhPortfolioSpec:
        if not isinstance(self.asset, SecurityKey):
            raise ValueError("TLH portfolios require a public security price, not private equity")
        return self


class MortgageInterestDeductionPolicy(BaseModel):
    """Mortgage-interest deduction (IRC §163(h)(3)) for one liability.

    At each tax-year-end, deductible interest =
    `liability_interest_ytd * min(1, principal_cap / origination_principal)`
    per jurisdiction. The qualifying interest from this policy is summed
    across all qualifying liabilities owned by the profile's agent and
    compared against the standard deduction; the engine uses
    `max(itemized, standard)` before bracket-walking.
    """

    liability_id: str
    owner_agent_id: AgentId
    debt_class: Literal["acquisition", "home_equity"] = Field(
        default="acquisition",
        description=(
            "§163(h)(3) classification. `acquisition` = loan used to buy, build, or "
            "substantially improve the secured home; interest is deductible up to the "
            "principal cap. `home_equity` = TCJA-period HELOC / second mortgage used "
            "for non-housing purposes; interest is not deductible (2018-2025) and the "
            "compiler holds this policy's MID ratio at 0. The IRS carve-out that "
            "re-classifies improvement-tied HELOCs back to acquisition is not modeled — "
            "tag improvement-tied HELOCs as `acquisition` if you want them deducted."
        ),
    )
    per_jurisdiction_principal_cap: dict[str, CurrencyAmount] = Field(
        default_factory=lambda: {"federal_us": Decimal(750_000), "california": Decimal(1_000_000)},
        description=(
            "Per-jurisdiction principal cap in USD. Federal post-TCJA caps acquisition "
            "debt at $750k; California's pre-TCJA $1M cap was preserved, so the two "
            "diverge for moderately-large mortgages."
        ),
    )
