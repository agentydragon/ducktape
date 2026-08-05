"""Scenario configuration — Pydantic models for the user-facing
config of a simulation run.

At spike 1, the scenario carries the agents, their initial cash
balances, a list of scheduled transfer events, and the horizon in
months. Later layers extend `Scenario` with positions (asset
holdings), liabilities (mortgages), properties, policies, the
external-series bundle reference, and tax profiles per agent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from finance.augur.model.series import IndexSeriesKey
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.product.asset_key import AssetKey, asset_price_key_or_none
from finance.augur.sim.cash_band import validate_band_bounds
from finance.augur.sim.enums import IncomeCategory
from finance.augur.sim.fixed_point import usd_to_cents
from finance.augur.sim.tlh_harvest import HarvestYieldParams


class FilingStatus(StrEnum):
    """Federal/state filing status. Today only single-filer is wired through the tax + §121
    math; adding a new variant requires touching every place that branches on filing status
    (bracket lookup keys in jurisdiction YAMLs, §121 cap table in `_apply_property_sale`,
    standard-deduction lookup, …). The enum makes this an explicit blocker on every
    callsite rather than a string typo silently falling through to a missing-key error."""

    SINGLE = "single"


class Agent(BaseModel):
    """An agent in the simulation. Identified by a stable id used
    on every frame keyed by agent_id."""

    agent_id: str


class InitialAccountBalance(BaseModel):
    """Starting cash for one (agent, account) pair at month 0."""

    agent_id: str
    account_id: str
    balance_usd: float


class FixedAmount(BaseModel):
    """A scalar dollar amount that does not vary by rollout or month."""

    kind: Literal["fixed"] = "fixed"
    amount_usd: float


class SeriesIndexedAmount(BaseModel):
    """A dollar amount pegged to a sampled external level series.

    The amount is `base_amount_usd` at `base_month_index`. For a
    payment due in month `m`, the simulator first snaps to the current
    adjustment period and then scales linearly by the model level ratio:

    `base_amount_usd * series[reset_month] / series[base_month_index]`.

    With `adjustment_period_months=12`, a rent obligation stays flat for
    the first lease year, resets at month 12, stays flat through month 23,
    and so on.

    `series` is a typed `IndexSeriesKey` (inflation or a location's rent) —
    the index whose level path scales the amount. Asset prices, home values,
    and PE marks are never amount indices, so the role type makes
    `series=SecurityKey(symbol=SP500_SYMBOL)` / `series=HomeValueKey(...)` a type error.
    """

    kind: Literal["series_indexed"] = "series_indexed"
    base_amount_usd: float
    series: IndexSeriesKey
    base_month_index: NonNegativeInt = 0
    adjustment_period_months: PositiveInt = 1

    def _reset_month(self, month: int) -> int:
        elapsed = month - self.base_month_index
        return self.base_month_index + (elapsed // self.adjustment_period_months) * self.adjustment_period_months


type AmountSchedule = Annotated[FixedAmount | SeriesIndexedAmount, Field(discriminator="kind")]
type AmountSpec = float | AmountSchedule


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


class ScheduledTransfer(BaseModel):
    """A cash transfer between two agents scheduled at a fixed
    month. Emitted by the engine as a Transfer event at that month;
    the amount may be fixed or derived from a series-indexed schedule.

    `income_category` tags the transfer as taxable income for the
    `to_agent_id` (recipient). When `"ordinary"`, the recipient's
    `ordinary_income_ytd` increments by the transferred amount —
    W-2-style wages, rental income, etc.

    `deduction_category` tags the transfer as a deductible expense for
    the `from_agent_id` (payer). When `"ordinary"`, the payer's
    `ordinary_income_ytd` decrements by the transferred amount —
    Schedule-E-style deductible expenses paid via transfer flows
    (property management fee, leasing fee, etc.). §469
    passive-activity loss limitations are not modeled. A transfer can
    carry both categories simultaneously (rare but legal — e.g.
    inter-company payment that is income to recipient and deductible
    by payer)."""

    month: int
    cause_id: str
    from_agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_usd: AmountSpec
    income_category: TransferIncomeCategory | None = None
    deduction_category: TransferDeductionCategory | None = None


class RecurringTransfer(BaseModel):
    """A cash transfer that fires every month within a window. The
    canonical use is a recurring paycheck (income arriving monthly)
    or recurring rent / utilities. The engine emits one Transfer
    event per active month per rollout; series-indexed amounts may
    vary by rollout and adjustment period.

    `start_month` is inclusive. `end_month` is inclusive when
    supplied; when `None`, the transfer fires through the scenario's
    horizon end. The `cause_id` is reused on every emitted event row
    so a user can group_by it to see "every paycheck Alice
    received"."""

    start_month: int
    end_month: int | None = None
    cause_id: str
    from_agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_usd: AmountSpec
    income_category: TransferIncomeCategory | None = None
    deduction_category: TransferDeductionCategory | None = None

    def is_active_at(self, month: int) -> bool:
        return self.start_month <= month and (self.end_month is None or month <= self.end_month)


class ScheduledPropertyCashflow(BaseModel):
    """A property-domain cashflow lowered to a transfer event while the property is active.

    Unlike generic `ScheduledTransfer`, this cashflow is tied to the referenced property's
    ownership lifecycle. It may be configured beyond sale; the engine suppresses it once the
    property is sold.
    """

    month: int
    property_id: str
    cause_id: str
    from_agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_usd: AmountSpec
    income_category: TransferIncomeCategory | None = None
    deduction_category: TransferDeductionCategory | None = None


class RecurringPropertyCashflow(BaseModel):
    """A recurring property-domain cashflow lowered to transfer events while active."""

    start_month: int
    end_month: int | None = None
    property_id: str
    cause_id: str
    from_agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_usd: AmountSpec
    income_category: TransferIncomeCategory | None = None
    deduction_category: TransferDeductionCategory | None = None

    def is_active_at(self, month: int) -> bool:
        return self.start_month <= month and (self.end_month is None or month <= self.end_month)


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


class ScheduledObligation(BaseModel):
    """A required due-now payment at one month.

    Unlike a raw transfer, an obligation is settled through the
    liquidity-policy path: available cash plus policy-emitted sale
    proceeds must cover the whole amount, and the rollout fails if
    the full amount cannot be paid immediately.

    `deduction_category` tags the (paid) amount as a tax-deductible
    expense for `agent_id`. When set, `agent_id`'s ordinary_income_ytd
    decrements by `deductible_fraction × paid_amount` at settlement
    time. `deductible_fraction` defaults to 1.0; smaller values model
    partial deductibility (e.g. the rented share of HOA dues on a
    partial rental).
    """

    month: int
    obligation_id: str
    obligation_type: str
    agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_due_usd: AmountSpec
    deduction_category: TransferDeductionCategory | None = None
    deductible_fraction: float = Field(default=1.0, ge=0.0, le=1.0)
    # When set, ties the obligation to a property; the engine then uses
    # `current.property_rented_fraction[r, prop]` at settlement time to override the
    # compile-time `deductible_fraction` (allowing mid-horizon lifecycle events to take
    # effect). Used today by HOA / insurance / maintenance flows on rented properties.
    property_id: str | None = None


class RecurringObligation(BaseModel):
    """A required due-now payment that repeats in a month window.

    `deduction_category` + `deductible_fraction` work the same way as on
    `ScheduledObligation` — see that class's docstring.
    """

    start_month: int
    end_month: int | None = None
    obligation_id: str
    obligation_type: str
    agent_id: str
    from_account_id: str
    to_agent_id: str
    to_account_id: str
    amount_due_usd: AmountSpec
    deduction_category: TransferDeductionCategory | None = None
    deductible_fraction: float = Field(default=1.0, ge=0.0, le=1.0)
    # When set, ties the obligation to a property; the engine uses
    # `current.property_rented_fraction[r, prop]` at settlement time to override the
    # compile-time `deductible_fraction` so mid-horizon lifecycle events take effect.
    property_id: str | None = None

    def is_active_at(self, month: int) -> bool:
        return self.start_month <= month and (self.end_month is None or month <= self.end_month)


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
    or premium cannot be valued or amortized — `purchase_price_usd` is required, and
    required to equal the face, so that a real holding bought at 98.5 raises instead of
    being silently treated as par.
    """

    model_config = ConfigDict(frozen=True)

    bond_id: str
    agent_id: str
    # No default: which account the coupons land in is a real decision, and a bond pointing
    # at an account that does not exist resolves to no slot at all — the coupon would be
    # scattered into the dump row and vanish silently rather than raise.
    account_id: str
    # The taxing authority that issued the debt — `federal_us` for a Treasury, `california`
    # for a CA muni, `None` for a corporate issuer. Whether any given holder owes tax on the
    # coupon is a relation between this issuer and that holder's jurisdictions, never a
    # property of the bond: "in-state" is holder-relative.
    issuer_jurisdiction_id: str | None = None
    face_value_usd: PositiveFloat
    purchase_price_usd: PositiveFloat
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
        # Compared in cents, not as floats: the scenario surface speaks in dollars, and two
        # amounts that are the same money can differ in binary floating point (a price that
        # round-trips through JSON as 99999.99999999999 is par). A cent is the precision the
        # rest of the engine accounts in, so it is the precision "at par" should mean.
        if usd_to_cents(self.purchase_price_usd) != usd_to_cents(self.face_value_usd):
            raise ValueError(
                f"bond {self.bond_id!r} was bought away from par "
                f"({self.purchase_price_usd=} vs {self.face_value_usd=}). Phase 1 supports par "
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


class InitialLot(BaseModel):
    """A tax lot that exists at scenario start. Models pre-existing
    holdings: Alice already owns 100 units of VTI bought 24 months
    before the sim starts at $80/unit. The sim creates this lot at
    month 0 as an `AssetPurchase` event with the supplied
    `purchase_month_index` (which may be negative — purchases
    pre-dating the horizon are fine and feed into LTCG/STCG
    classification of later sales). `account_id` identifies the
    holding account used for FIFO pools; lots in different accounts
    are not fungible.

    `asset` is the typed `AssetKey` discriminated union identifying what
    is held (sp500 / a crypto symbol / a PE issuer). Dispatch sites match
    on it with `isinstance`; the compiler derives the lot's pricing series
    from it via `asset_price_key`.
    """

    lot_id: str
    agent_id: str
    account_id: str = "checking"
    asset: AssetKey
    purchase_month_index: int
    quantity: float
    cost_basis_per_unit_usd: float


class ScheduledAssetSale(BaseModel):
    """Sell a configured quantity of an asset at a fixed month. The
    sale consumes from the agent's lots of that asset in
    `source_account_id` in FIFO order by `purchase_month_index`.
    Proceeds = `quantity * unit_price` are credited to
    `proceeds_account_id`.

    `price_per_unit_usd` is optional: when supplied the sale uses
    that price uniformly across rollouts (useful for deterministic
    tests). When `None`, the per-rollout per-month price comes from
    the scenario's `SeriesModelBundle` — the canonical case once external
    series integration is in play."""

    month: int
    cause_id: str
    agent_id: str
    source_account_id: str = "checking"
    asset: AssetKey
    quantity: float
    proceeds_account_id: str
    price_per_unit_usd: float | None = None


class ScheduledAssetPurchase(BaseModel):
    """Buy a dollar amount of an asset at a fixed month, funded from a cash account.

    CLEANUP(added 2026-08-05): Fold into the actor policy and delete this type once that
      policy emits buy actions (#3739). A scheduled buy is just a policy that ignores
      state, so keeping both leaves two channels through which an agent transacts — and
      they will drift on ordering against obligations, on the underfunding clamp, on basis,
      and on what lands in the event log. Only this config type goes: the execution layer
      below it (pre-allocated lot slots, per-rollout basis, whole-quanta rounding, the
      external contra credit) is what the policy emits INTO, and is the point of it.
      It survives here in the meantime because the mechanism needs a trigger to be testable
      at all, and an untested substrate would be the worse trade.

    The mirror of `ScheduledAssetSale`, and the only way a tax lot comes into existence
    mid-horizon. Two things make it more than a sale with the sign flipped, and both are
    why it needs its own machinery: the quantity is not known at compile time (it is
    `amount / price`, and the price is a sampled path), and neither is the resulting
    lot's cost basis. The compiler allocates the lot slot; what fills it is decided per
    rollout.

    Whole quanta only. The purchase takes `floor(amount * scale / price)` quanta and
    debits exactly what those cost, leaving the sub-quantum remainder in the funding
    account. Rounding units up instead would debit cash that bought nothing.

    Underfunding CLAMPS rather than fails: a month where the account holds less than
    `amount_usd` buys what the cash covers. That is not a silent loss — the executed
    amount is on the purchase event, so a caller comparing it against `amount_usd` sees
    the shortfall — and it is the same semantics a buying policy needs in step 2, where
    "invest the surplus" is inherently sized by what is there.
    """

    month: int
    cause_id: str
    lot_id: str = Field(description="Identity of the lot this creates; must not collide with an initial lot.")
    agent_id: str
    from_account_id: str = Field(default="checking", description="Cash account debited.")
    to_account_id: str = Field(
        default="checking", description="Holding account the lot lands in; lots in different accounts are not fungible."
    )
    asset: AssetKey
    amount_usd: PositiveFloat
    # As on `ScheduledAssetSale`: fixed price for deterministic tests, sampled series when None.
    price_per_unit_usd: float | None = None


class LiquidityPolicy(BaseModel):
    """Asset-sale policy for one agent cash account.

    Required obligations create cash demands, but the policy decides
    whether and how to sell assets to fund them. If a policy emits no
    sale orders, the settlement phase will fail any hard demand that
    cash cannot already cover, even when the agent owns sellable
    assets. Optional cash-buffer rules run after hard demands are
    accounted for and never cause failure by themselves.
    """

    agent_id: str
    # Cash account that receives sale proceeds and pays matching obligations.
    account_id: str
    # Holding accounts the policy may liquidate. Empty preserves the original behavior:
    # sell only lots already in `account_id`.
    source_account_ids: tuple[str, ...] = ()
    asset_preference_chain: list[AssetKey]
    # `AmountSpec = float | AmountSchedule` — pass a raw float for a constant buffer, or a
    # `SeriesIndexedAmount` (e.g. `series=InflationKey()`) to keep the buffer in real terms.
    cash_buffer_trigger_below_usd: AmountSpec = 0.0
    cash_buffer_sale_usd: AmountSpec = 0.0
    cause_id_prefix: str = "liquidity_sale"


class SleeveTarget(BaseModel):
    """One sleeve of a target allocation: an asset and its relative weight.

    Weights are integers and only their RATIOS matter — `(3, 1)` and `(30, 10)` are the same
    policy. A fraction would be derivable from the weights, so storing fractions would store
    a computed quantity and need a float sum-to-one validator to defend it.
    """

    asset: AssetKey
    weight: PositiveInt


class TargetAllocationPolicy(BaseModel):
    """Funding policy for one agent cash account: hold cash in a band, sell toward a target.

    Replaces `LiquidityPolicy`'s ordered sell-list with a target the sales move TOWARD. When
    the account's projected end-of-month balance falls below `cash_floor_usd`, the policy
    raises enough to reach `cash_ceiling_usd`, taking from the most overweight sleeve first
    so what remains is as close to the target ratios as the raise allows.

    The band is (s,S): crossing the floor refills to the ceiling, not back to the floor.
    Refilling to the floor would put the agent back at its trigger next month, making it a
    forced seller into every dip — which is the risk this whole model exists to price.

    **The ceiling is the refill TARGET, not an invest-above-this rule.** Surplus cash above
    it accumulates; nothing buys with it. Investing surplus has never existed in augur and
    this policy does not add it — that arrives with policy-driven purchases, and only then
    does the ceiling gain a second meaning.

    Sleeves the policy does not name are outside the target denominator entirely: never sold
    to fund the band, and not counted when measuring what is overweight. That is what makes
    a target alongside an untradeable holding — private equity before liquidity, a bond that
    will be held to maturity — expressible at all.
    """

    agent_id: str
    # Cash account the band governs: it receives sale proceeds and pays the matching obligations.
    account_id: str
    # Holding accounts the policy may sell from. Empty means the funding account only.
    source_account_ids: tuple[str, ...] = ()
    sleeves: list[SleeveTarget]
    # `AmountSpec = float | AmountSchedule` — a raw float for a constant band, or a
    # `SeriesIndexedAmount` (e.g. `series=InflationKey()`) to hold the band in real terms.
    cash_floor_usd: AmountSpec = 0.0
    cash_ceiling_usd: AmountSpec
    cause_id_prefix: str = "allocation_sale"

    @model_validator(mode="after")
    def _reject_duplicate_and_inverted(self) -> TargetAllocationPolicy:
        if not self.sleeves:
            raise ValueError(
                f"target-allocation policy for {self.agent_id}/{self.account_id} names no sleeves; "
                "a policy with an empty target can never raise cash and would fail every obligation "
                "the account cannot already cover"
            )
        assets = [sleeve.asset for sleeve in self.sleeves]
        if len(set(assets)) != len(assets):
            duplicated = sorted({str(asset) for asset in assets if assets.count(asset) > 1})
            raise ValueError(
                f"target-allocation policy for {self.agent_id}/{self.account_id} names {duplicated} "
                "more than once; an asset weighted twice is counted twice and skews every target"
            )
        # Band ordering is checked on the CONFIGURED amounts because per-month values may be
        # CPI-indexed, hence traced, and a traced value cannot drive a raise. Indexing scales
        # both bounds by the same series, so an ordering that holds here holds on every path.
        validate_band_bounds(
            floor_usd=_base_amount_usd(self.cash_floor_usd), ceiling_usd=_base_amount_usd(self.cash_ceiling_usd)
        )
        return self


def _base_amount_usd(spec: AmountSpec) -> float:
    """The configured base of an amount spec, for compile-time checks that compare two specs."""

    match spec:
        case float() | int():
            return float(spec)
        case FixedAmount():
            return spec.amount_usd
        case SeriesIndexedAmount():
            return spec.base_amount_usd


class TaxProfile(BaseModel):
    """A taxed agent's tax-time configuration. At spike 1 only single filers are modeled;
    later layers add MFJ / HoH and any filing-status-driven branching."""

    agent_id: str
    filing_status: FilingStatus = FilingStatus.SINGLE
    jurisdiction_ids: list[str] = Field(
        description='Ordered list of taxing authorities — typically `["federal_us", "california"]` for a CA resident.'
    )
    tax_authority_agent_id: str = Field(
        description="Destination of tax-payment transfers — a bookkeeping sink, not a taxed agent itself."
    )
    payment_account_id: str = Field(
        default="checking", description="The agent's account the engine debits for estimated-tax and true-up payments."
    )
    tax_authority_account_id: str = Field(
        default="checking", description="The matching credit account on the tax authority's side."
    )
    prior_year_tax_usd: float = Field(
        default=0.0,
        description=(
            "Aggregate safe-harbor target used to size quarterly estimated payments. If "
            "0, no quarterly estimates are emitted and the January true-up pays the full "
            "accrued tax."
        ),
    )


class MortgageFinancing(BaseModel):
    """Mortgage terms attached to a property purchase."""

    liability_id: str
    lender_agent_id: str
    lender_account_id: str = "checking"
    principal_usd: float
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

    agent_id: str
    property_id: str


class SetPrimaryResidenceEvent(BaseModel):
    """Mid-horizon transition: assign or clear an agent's primary residence."""

    kind: Literal["set_primary_residence"] = "set_primary_residence"
    month: int
    agent_id: str
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

    Debits the property owner's cash by `amount_usd` and increases the property's depreciable
    building basis by the same amount. Future depreciation accrues on the new (higher) basis.
    The new improvement is treated as adding to the existing depreciation track rather than
    starting a separate 27.5-year clock (a simplification — cost-segregation studies in
    practice can split improvements into 5/7/15/27.5 year buckets; out of scope here).
    """

    kind: Literal["capital_improvement"] = "capital_improvement"
    month: int
    property_id: str
    amount_usd: float = Field(gt=0.0)
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
    buyer_agent_id: str
    buyer_account_id: str
    seller_agent_id: str
    seller_account_id: str = "checking"
    purchase_price_usd: float
    down_payment_usd: float
    buyer_closing_cost_usd: float = 0.0
    mortgage: MortgageFinancing | None = None
    rented_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    # Tax-assessor split between land (non-depreciable) and building (depreciable, 27.5-year
    # straight-line under §168). Default 0.20 (20% land / 80% building) is a common
    # cost-segregation rule of thumb absent assessor data. The engine accrues monthly
    # depreciation = `building_basis × rented_fraction / (27.5 × 12)` where building_basis =
    # `purchase_price_usd × (1 - land_value_fraction) + buyer_closing_cost_usd`.
    land_value_fraction: float = Field(default=0.20, ge=0.0, le=1.0)


class PropertyTaxPolicy(BaseModel):
    """Monthly property-tax carrying cost for an owned property.

    `annual_tax_rate` can override location reference data; when it
    is `None`, the rate comes from `Location.annual_property_tax_rate`.
    """

    property_id: str
    owner_agent_id: str
    from_account_id: str = "checking"
    tax_authority_agent_id: str
    tax_authority_account_id: str = "checking"
    annual_tax_rate: float | None = None
    start_month: int = 0
    end_month: int | None = None

    def is_active_at(self, month: int) -> bool:
        return self.start_month <= month and (self.end_month is None or month <= self.end_month)


class FederalSaltCapEntry(BaseModel):
    """One step of the federal SALT-cap schedule.

    The cap that applies in calendar-year-index `Y` is the `cap_usd` of the
    latest entry with `effective_year_index <= Y`. Year-index is 0-based from
    the start of the simulation horizon, so [(0, 40_000.0), (4, 10_000.0)]
    encodes "$40k for years 0..3, then $10k from year 4 onward" — the OBBBA
    transition for a 2026-start sim ($40k for 2026..2029, $10k from 2030).
    """

    effective_year_index: int
    cap_usd: float


# Default schedule reflects the TCJA + OBBBA federal SALT-cap timeline as
# enacted through mid-2025: $40k cap for 2025..2029 (current sims start in 2026
# so year index 0..3), reverting to $10k from 2030 onward. Sims that span the
# transition see the cap tighten mid-horizon.
#
# Known modeling gaps (not implemented; document so the consumer knows what
# we elide):
#   - **AGI-based phase-out of the $40k cap.** The OBBBA cap phases down for
#     high incomes (over ~$500k AGI); we treat it as a flat ceiling.
#   - **Sales tax election.** Taxpayers in no-state-income-tax states may
#     deduct state sales tax instead of state income tax; we always use the
#     accrued state income tax.
#   - **Timing nuance.** Real Schedule A allows deducting state taxes *paid*
#     in the calendar year, which can include prior-year true-ups. We deduct
#     state tax *accrued in this calendar year* (equivalent to assuming all
#     state tax is withheld in the year of accrual).
#   - **Standalone post-2029 sunset.** If Congress doesn't extend OBBBA,
#     2030+ reverts to the TCJA $10k cap — already reflected here. If the
#     entire TCJA sunset triggers, the cap disappears (deduction becomes
#     unlimited); express that by passing an empty schedule (no entries =
#     no cap = uncapped SALT deduction).
DEFAULT_FEDERAL_SALT_CAP_SCHEDULE: tuple[FederalSaltCapEntry, ...] = (
    FederalSaltCapEntry(effective_year_index=0, cap_usd=40_000.0),
    FederalSaltCapEntry(effective_year_index=4, cap_usd=10_000.0),
)


class FederalSaltDeductionPolicy(BaseModel):
    """Federal SALT deduction (Schedule A) for one tax profile.

    SALT contributors are derived from the targeted `TaxProfile`: every
    jurisdiction in `TaxProfile.jurisdiction_ids` other than
    `federal_jurisdiction_id` is treated as a state/local jurisdiction whose
    annual accrued income tax flows into the federal SALT total, alongside
    property tax paid this calendar year by the profile's agent. The total
    is capped per `cap_schedule` and surfaces as a federal itemized line
    that stacks with MID.

    `federal_jurisdiction_id` is the jurisdiction within the profile that
    *receives* the SALT deduction; the SALT cap is a federal-Schedule-A
    concept and only applies there. Default `federal_us` matches the
    convention used by `local_regulation.py` and `tax_profile_defaults`.
    """

    profile_id: str
    federal_jurisdiction_id: str = "federal_us"
    cap_schedule: list[FederalSaltCapEntry] = Field(default_factory=lambda: list(DEFAULT_FEDERAL_SALT_CAP_SCHEDULE))


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

    owner_agent_id: str
    proceeds_account_id: str = "checking"
    liquid_net_worth_floor: AmountSchedule


class HarvestPolicy(BaseModel):
    """Attach a reduced-form tax-loss-harvesting (TLH) process to one index-tracking holding.

    LIMITED / DELIBERATELY-APPROXIMATE ("UNTRUTHFUL") MODEL — read before relying on output.
    This does NOT simulate the real direct-indexing sleeve's constituent stocks. The holding
    stays a single index-tracking position; the "harvested loss" each month is a *calibrated
    function* of the index path (see `augur/sim/tlh_harvest.py`), not a real below-basis amount
    realized by selling specific underwater names. All `HarvestYieldParams` are `[HEURISTIC]`,
    anchored only to the account's first-year (TY2025) 1099-B. See the engine phase
    `_apply_tlh_harvest` for the full rationale; `finance/augur/sim/TODO.md`
    tracks the more honest representative-sleeve upgrade path.

    The policy is keyed to the lots of one (agent, account, asset) pool — typically the Plaid
    SP500 proxy sleeve. Each month the engine harvests a calibrated capital LOSS into that
    owner's `capital_gain_ytd` (Piece-1 netting then nets it like any other realized loss) and
    accumulates the harvested total into a single scalar `tlh_cumulative_harvest` per
    (policy, rollout). That scalar lowers the holding's adjusted basis, which (a) raises the
    embedded-gain fraction so the yield decays toward its floor ("ossification"), and (b) is
    GIVEN BACK at sale time: any realized gain on this pool's lots uses the *reduced* basis, so
    the deferred gain is honestly repaid. The net benefit is therefore bounded deferral +
    rate-arbitrage + the $3k/yr ordinary offset — never free money.
    """

    owner_agent_id: str
    account_id: str = "checking"
    asset: AssetKey = Field(description="Index-tracking asset whose lots this policy harvests (e.g. a SecurityKey).")
    yield_params: HarvestYieldParams
    short_term_fraction: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Share of each month's harvested loss booked as short-term (the rest is long-term). "
            "Seeded from the holding-period buckets — near 1.0 for a young account, matching the "
            "TY2025 1099-B's essentially-all-short-term harvest. [HEURISTIC]."
        ),
    )


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
    owner_agent_id: str
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
    per_jurisdiction_principal_cap_usd: dict[str, float] = Field(
        default_factory=lambda: {"federal_us": 750_000.0, "california": 1_000_000.0},
        description=(
            "Per-jurisdiction principal cap in USD. Federal post-TCJA caps acquisition "
            "debt at $750k; California's pre-TCJA $1M cap was preserved, so the two "
            "diverge for moderately-large mortgages."
        ),
    )


class Scenario(BaseModel):
    """Spike-1 simulation scenario. Carries the minimum to run
    a multi-rollout simulation over a fixed horizon with both
    scheduled and recurring transfers, plus tax lots and asset
    sales."""

    agents: list[Agent]
    initial_cash: list[InitialAccountBalance]
    initial_lots: list[InitialLot] = Field(default_factory=list)
    initial_bonds: list[BondHolding] = Field(default_factory=list)
    scheduled_transfers: list[ScheduledTransfer] = Field(default_factory=list)
    recurring_transfers: list[RecurringTransfer] = Field(default_factory=list)
    scheduled_property_cashflows: list[ScheduledPropertyCashflow] = Field(default_factory=list)
    recurring_property_cashflows: list[RecurringPropertyCashflow] = Field(default_factory=list)
    scheduled_obligations: list[ScheduledObligation] = Field(default_factory=list)
    recurring_obligations: list[RecurringObligation] = Field(default_factory=list)
    scheduled_asset_sales: list[ScheduledAssetSale] = Field(default_factory=list)
    scheduled_asset_purchases: list[ScheduledAssetPurchase] = Field(default_factory=list)
    scheduled_property_purchases: list[ScheduledPropertyPurchase] = Field(default_factory=list)
    initial_primary_residences: list[PrimaryResidenceAssignment] = Field(default_factory=list)
    primary_residence_events: list[SetPrimaryResidenceEvent] = Field(default_factory=list)
    # Mid-horizon transitions that mutate per-property rented_fraction at runtime. Each event
    # must reference an existing property_id from scheduled_property_purchases and fire after
    # that property's purchase month.
    property_lifecycle_events: list[PropertyLifecycleEvent] = Field(default_factory=list)
    property_tax_policies: list[PropertyTaxPolicy] = Field(default_factory=list)
    mortgage_interest_deduction_policies: list[MortgageInterestDeductionPolicy] = Field(default_factory=list)
    federal_salt_deduction_policies: list[FederalSaltDeductionPolicy] = Field(default_factory=list)
    private_equity_tender_policies: list[PrivateEquityTenderPolicy] = Field(default_factory=list)
    # Reduced-form TLH harvest processes attached to index-tracking holdings (Piece 2). Empty by
    # default, so scenarios without harvesting reproduce prior behavior exactly. See HarvestPolicy.
    harvest_policies: list[HarvestPolicy] = Field(default_factory=list)
    external_series: SeriesModelBundle = Field(default_factory=SeriesModelBundle)
    # Required so callers explicitly choose either taxed agents or an intentional no-tax scenario.
    tax_profiles: list[TaxProfile]
    liquidity_policies: list[LiquidityPolicy] = Field(default_factory=list)
    target_allocation_policies: list[TargetAllocationPolicy] = Field(default_factory=list)
    horizon_months: PositiveInt

    @model_validator(mode="after")
    def _reject_duplicate_agent_ids(self) -> Scenario:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for agent in self.agents:
            if agent.agent_id in seen:
                duplicates.add(agent.agent_id)
            seen.add(agent.agent_id)
        if duplicates:
            duplicate_list = ", ".join(repr(agent_id) for agent_id in sorted(duplicates))
            raise ValueError(f"duplicate agent_id(s): {duplicate_list}")
        return self

    @model_validator(mode="after")
    def _reject_duplicate_initial_lot_purchase_months(self) -> Scenario:
        seen: dict[tuple[str, str, str, int], str] = {}
        duplicates: list[tuple[str, str, str, int, str, str]] = []
        for lot in self.initial_lots:
            # Dedup/sort/message on the asset's wire id (a stable, ordered string id) — the
            # tuple stays string-keyed so `sorted(duplicates)` compares cleanly.
            key = (lot.agent_id, lot.account_id, lot.asset.wire_id, lot.purchase_month_index)
            previous_lot_id = seen.get(key)
            if previous_lot_id is not None:
                duplicates.append((*key, previous_lot_id, lot.lot_id))
            else:
                seen[key] = lot.lot_id
        if duplicates:
            duplicate_list = ", ".join(
                f"{agent_id}/{account_id}/{asset_id}@{purchase_month} ({first_lot_id}, {second_lot_id})"
                for agent_id, account_id, asset_id, purchase_month, first_lot_id, second_lot_id in sorted(duplicates)
            )
            raise ValueError(f"duplicate initial lot purchase months for FIFO pool(s): {duplicate_list}")
        return self

    @model_validator(mode="after")
    def _reject_unusable_scheduled_asset_purchases(self) -> Scenario:
        """Lot ids must stay unique, and the asset must have a price the engine can read.

        Private equity is marked by `pe_channels`, not by the price cube, so there is no
        per-month price to divide an amount by — a PE purchase would have no defined
        quantity. Rejecting it here beats a NO_CODE series index surfacing as a zero
        price and a silently free infinite position.
        """

        lot_ids = {lot.lot_id for lot in self.initial_lots}
        for purchase in self.scheduled_asset_purchases:
            if purchase.lot_id in lot_ids:
                raise ValueError(
                    f"scheduled asset purchase {purchase.cause_id!r} reuses {purchase.lot_id=}, "
                    "which already names another lot"
                )
            lot_ids.add(purchase.lot_id)
            if asset_price_key_or_none(purchase.asset) is None:
                raise ValueError(
                    f"scheduled asset purchase {purchase.cause_id!r} buys {purchase.asset.wire_id!r}, "
                    "which has no price series (private equity is marked, not priced); "
                    "model an acquisition of it as an initial lot instead"
                )
        return self

    @model_validator(mode="after")
    def _reject_out_of_horizon_scheduled_events(self) -> Scenario:
        horizon = int(self.horizon_months)
        for scheduled_transfer in self.scheduled_transfers:
            if not 0 <= scheduled_transfer.month < horizon:
                raise ValueError(
                    f"scheduled transfer {scheduled_transfer.cause_id!r} "
                    f"has month {scheduled_transfer.month}, "
                    f"outside scenario horizon [0, {horizon})"
                )
        for sale in self.scheduled_asset_sales:
            if not 0 <= sale.month < horizon:
                raise ValueError(
                    f"scheduled asset sale {sale.cause_id!r} has month {sale.month}, "
                    f"outside scenario horizon [0, {horizon})"
                )
        for asset_purchase in self.scheduled_asset_purchases:
            if not 0 <= asset_purchase.month < horizon:
                raise ValueError(
                    f"scheduled asset purchase {asset_purchase.cause_id!r} has month {asset_purchase.month}, "
                    f"outside scenario horizon [0, {horizon})"
                )
        for scheduled_obligation in self.scheduled_obligations:
            if not 0 <= scheduled_obligation.month < horizon:
                raise ValueError(
                    f"scheduled obligation {scheduled_obligation.obligation_id!r} "
                    f"has month {scheduled_obligation.month}, "
                    f"outside scenario horizon [0, {horizon})"
                )
        for scheduled_cashflow in self.scheduled_property_cashflows:
            if not 0 <= scheduled_cashflow.month < horizon:
                raise ValueError(
                    f"scheduled property cashflow {scheduled_cashflow.cause_id!r} "
                    f"has month {scheduled_cashflow.month}, "
                    f"outside scenario horizon [0, {horizon})"
                )
        for purchase in self.scheduled_property_purchases:
            if not 0 <= purchase.month < horizon:
                raise ValueError(
                    f"scheduled property purchase {purchase.cause_id!r} has month {purchase.month}, "
                    f"outside scenario horizon [0, {horizon})"
                )
        for recurring_transfer in self.recurring_transfers:
            if (
                recurring_transfer.end_month is not None
                and recurring_transfer.end_month < recurring_transfer.start_month
            ):
                raise ValueError(
                    f"recurring transfer {recurring_transfer.cause_id!r} "
                    f"has end_month {recurring_transfer.end_month} "
                    f"before start_month {recurring_transfer.start_month}"
                )
        for recurring_cashflow in self.recurring_property_cashflows:
            if (
                recurring_cashflow.end_month is not None
                and recurring_cashflow.end_month < recurring_cashflow.start_month
            ):
                raise ValueError(
                    f"recurring property cashflow {recurring_cashflow.cause_id!r} "
                    f"has end_month {recurring_cashflow.end_month} "
                    f"before start_month {recurring_cashflow.start_month}"
                )
        for recurring_obligation in self.recurring_obligations:
            if (
                recurring_obligation.end_month is not None
                and recurring_obligation.end_month < recurring_obligation.start_month
            ):
                raise ValueError(
                    f"recurring obligation {recurring_obligation.obligation_id!r} "
                    f"has end_month {recurring_obligation.end_month} "
                    f"before start_month {recurring_obligation.start_month}"
                )
        return self

    @model_validator(mode="after")
    def _reject_invalid_property_lifecycle_events(self) -> Scenario:
        horizon = int(self.horizon_months)
        purchase_month_by_property_id: dict[str, int] = {}
        duplicate_property_ids: set[str] = set()
        for purchase in self.scheduled_property_purchases:
            if purchase.property_id in purchase_month_by_property_id:
                duplicate_property_ids.add(purchase.property_id)
            purchase_month_by_property_id[purchase.property_id] = int(purchase.month)
        if duplicate_property_ids:
            duplicate_list = ", ".join(repr(property_id) for property_id in sorted(duplicate_property_ids))
            raise ValueError(f"duplicate scheduled property purchase property_id(s): {duplicate_list}")

        property_cashflows: list[ScheduledPropertyCashflow | RecurringPropertyCashflow] = [
            *self.scheduled_property_cashflows,
            *self.recurring_property_cashflows,
        ]
        for cashflow in property_cashflows:
            if cashflow.property_id not in purchase_month_by_property_id:
                known = ", ".join(repr(property_id) for property_id in sorted(purchase_month_by_property_id))
                raise ValueError(
                    f"property cashflow {cashflow.cause_id!r} references unknown property_id "
                    f"{cashflow.property_id!r}; known: {known or '<none>'}"
                )

        sale_month_by_property_id: dict[str, int] = {}
        lifecycle_events_by_property_month: dict[tuple[str, int], list[PropertyLifecycleEvent]] = {}
        for lifecycle_event in self.property_lifecycle_events:
            event_month = int(lifecycle_event.month)
            lifecycle_events_by_property_month.setdefault((lifecycle_event.property_id, event_month), []).append(
                lifecycle_event
            )
            if not 0 <= event_month < horizon:
                raise ValueError(
                    f"property lifecycle event for {lifecycle_event.property_id!r} "
                    f"has month {lifecycle_event.month}, "
                    f"outside scenario horizon [0, {horizon})"
                )
            purchase_month = purchase_month_by_property_id.get(lifecycle_event.property_id)
            if purchase_month is None:
                known = ", ".join(repr(property_id) for property_id in sorted(purchase_month_by_property_id))
                raise ValueError(
                    f"property lifecycle event at month {lifecycle_event.month} references unknown property_id "
                    f"{lifecycle_event.property_id!r}; known: {known or '<none>'}"
                )
            if event_month <= purchase_month:
                raise ValueError(
                    f"property lifecycle event for {lifecycle_event.property_id!r} "
                    f"fires at month {lifecycle_event.month} "
                    f"but the property's purchase month is {purchase_month}; lifecycle events must "
                    "fire strictly after purchase."
                )
            if isinstance(lifecycle_event, PropertySaleEvent):
                previous_sale_month = sale_month_by_property_id.get(lifecycle_event.property_id)
                if previous_sale_month is not None:
                    raise ValueError(
                        f"multiple property sale lifecycle events for {lifecycle_event.property_id!r}: "
                        f"months {previous_sale_month} and {lifecycle_event.month}"
                    )
                sale_month_by_property_id[lifecycle_event.property_id] = event_month

        for (property_id, event_month), lifecycle_events in lifecycle_events_by_property_month.items():
            sale_events = [event for event in lifecycle_events if isinstance(event, PropertySaleEvent)]
            if not sale_events:
                continue
            non_sale_events = [event for event in lifecycle_events if not isinstance(event, PropertySaleEvent)]
            if non_sale_events:
                other_types = ", ".join(sorted(type(event).__name__ for event in non_sale_events))
                raise ValueError(
                    f"property lifecycle events for {property_id!r} at month {event_month} combine "
                    f"PropertySaleEvent with {other_types}; same-month sale lifecycle ordering is ambiguous"
                )

        for lifecycle_event in self.property_lifecycle_events:
            sale_month = sale_month_by_property_id.get(lifecycle_event.property_id)
            if sale_month is None:
                continue
            event_month = int(lifecycle_event.month)
            if event_month > sale_month or (
                event_month == sale_month and not isinstance(lifecycle_event, PropertySaleEvent)
            ):
                raise ValueError(
                    f"property lifecycle event for {lifecycle_event.property_id!r} at month {lifecycle_event.month} "
                    f"fires after sale at month {sale_month}; the property is frozen after sale"
                )
        return self

    @model_validator(mode="after")
    def _reject_invalid_primary_residence_assignments(self) -> Scenario:
        horizon = int(self.horizon_months)
        agent_ids = {agent.agent_id for agent in self.agents}
        purchase_by_property_id: dict[str, ScheduledPropertyPurchase] = {}
        for purchase in self.scheduled_property_purchases:
            purchase_by_property_id[purchase.property_id] = purchase

        sale_month_by_property_id: dict[str, int] = {}
        for lifecycle_event in self.property_lifecycle_events:
            if isinstance(lifecycle_event, PropertySaleEvent):
                sale_month_by_property_id[lifecycle_event.property_id] = int(lifecycle_event.month)

        seen_initial_agents: set[str] = set()
        for assignment in self.initial_primary_residences:
            if assignment.agent_id in seen_initial_agents:
                raise ValueError(f"multiple initial primary residences for agent_id {assignment.agent_id!r}")
            seen_initial_agents.add(assignment.agent_id)
            self._validate_primary_residence_property_assignment(
                label="initial primary residence",
                agent_id=assignment.agent_id,
                property_id=assignment.property_id,
                month=0,
                agent_ids=agent_ids,
                purchase_by_property_id=purchase_by_property_id,
                sale_month_by_property_id=sale_month_by_property_id,
                allow_same_month_purchase=True,
            )

        seen_event_keys: set[tuple[str, int]] = set()
        for primary_event in self.primary_residence_events:
            event_month = int(primary_event.month)
            if not 0 <= event_month < horizon:
                raise ValueError(
                    f"primary residence event for agent_id {primary_event.agent_id!r} "
                    f"has month {primary_event.month}, "
                    f"outside scenario horizon [0, {horizon})"
                )
            key = (primary_event.agent_id, event_month)
            if key in seen_event_keys:
                raise ValueError(
                    f"multiple primary residence events for agent_id {primary_event.agent_id!r} "
                    f"at month {primary_event.month}"
                )
            seen_event_keys.add(key)
            if primary_event.property_id is None:
                if primary_event.agent_id not in agent_ids:
                    known = ", ".join(repr(agent_id) for agent_id in sorted(agent_ids))
                    raise ValueError(
                        f"primary residence event at month {primary_event.month} references unknown agent_id "
                        f"{primary_event.agent_id!r}; known: {known or '<none>'}"
                    )
                continue
            self._validate_primary_residence_property_assignment(
                label="primary residence event",
                agent_id=primary_event.agent_id,
                property_id=primary_event.property_id,
                month=event_month,
                agent_ids=agent_ids,
                purchase_by_property_id=purchase_by_property_id,
                sale_month_by_property_id=sale_month_by_property_id,
                allow_same_month_purchase=True,
            )
        return self

    def _validate_primary_residence_property_assignment(
        self,
        *,
        label: str,
        agent_id: str,
        property_id: str,
        month: int,
        agent_ids: set[str],
        purchase_by_property_id: dict[str, ScheduledPropertyPurchase],
        sale_month_by_property_id: dict[str, int],
        allow_same_month_purchase: bool,
    ) -> None:
        if agent_id not in agent_ids:
            known = ", ".join(repr(agent) for agent in sorted(agent_ids))
            raise ValueError(f"{label} references unknown agent_id {agent_id!r}; known: {known or '<none>'}")
        purchase = purchase_by_property_id.get(property_id)
        if purchase is None:
            known = ", ".join(repr(property_id) for property_id in sorted(purchase_by_property_id))
            raise ValueError(f"{label} references unknown property_id {property_id!r}; known: {known or '<none>'}")
        if purchase.buyer_agent_id != agent_id:
            raise ValueError(
                f"{label} assigns property_id {property_id!r} to agent_id {agent_id!r}, "
                f"but the property's buyer_agent_id is {purchase.buyer_agent_id!r}"
            )
        purchase_month = int(purchase.month)
        if month < purchase_month or (month == purchase_month and not allow_same_month_purchase):
            raise ValueError(
                f"{label} assigns property_id {property_id!r} at month {month}, "
                f"before its purchase month {purchase_month}"
            )
        sale_month = sale_month_by_property_id.get(property_id)
        if sale_month is not None and month > sale_month:
            raise ValueError(
                f"{label} assigns property_id {property_id!r} at month {month}, after sale at month {sale_month}"
            )

    @model_validator(mode="after")
    def _reject_duplicate_funding_policy_accounts(self) -> Scenario:
        """One funding policy per cash account, counting both kinds together.

        Two policies on one account would each size their raise from the same projected
        balance, unaware of the other's sale, and between them sell roughly twice what the
        month needed. Mixing the two KINDS on one account is the same bug wearing a disguise,
        which is why they share a namespace rather than being checked separately.
        """

        # Keyed separately rather than over a merged sequence: the two policy types share no
        # base class narrower than BaseModel, so a merged iteration loses the attributes.
        keys = [(policy.agent_id, policy.account_id) for policy in self.liquidity_policies] + [
            (policy.agent_id, policy.account_id) for policy in self.target_allocation_policies
        ]
        seen: set[tuple[str, str]] = set()
        duplicates: set[tuple[str, str]] = set()
        for key in keys:
            if key in seen:
                duplicates.add(key)
            seen.add(key)
        if duplicates:
            duplicate_list = ", ".join(f"{agent_id}/{account_id}" for agent_id, account_id in sorted(duplicates))
            raise ValueError(f"duplicate funding policies for account(s): {duplicate_list}")
        return self

    @model_validator(mode="after")
    def _reject_ambiguous_tax_and_liability_links(self) -> Scenario:
        seen_tax_profile_agents: set[str] = set()
        duplicate_tax_profile_agents: set[str] = set()
        for profile in self.tax_profiles:
            if profile.agent_id in seen_tax_profile_agents:
                duplicate_tax_profile_agents.add(profile.agent_id)
            seen_tax_profile_agents.add(profile.agent_id)
        if duplicate_tax_profile_agents:
            duplicate_list = ", ".join(repr(agent_id) for agent_id in sorted(duplicate_tax_profile_agents))
            raise ValueError(f"duplicate TaxProfile.agent_id(s): {duplicate_list}")

        purchase_by_property_id = {purchase.property_id: purchase for purchase in self.scheduled_property_purchases}
        seen_liability_ids: dict[str, str] = {}
        duplicate_liability_ids: list[tuple[str, str, str]] = []
        for purchase in self.scheduled_property_purchases:
            if purchase.mortgage is None:
                continue
            liability_id = purchase.mortgage.liability_id
            previous_property_id = seen_liability_ids.get(liability_id)
            if previous_property_id is not None:
                duplicate_liability_ids.append((liability_id, previous_property_id, purchase.property_id))
            else:
                seen_liability_ids[liability_id] = purchase.property_id
        if duplicate_liability_ids:
            duplicate_list = ", ".join(
                f"{liability_id!r} on {first_property_id!r} and {second_property_id!r}"
                for liability_id, first_property_id, second_property_id in sorted(duplicate_liability_ids)
            )
            raise ValueError(f"duplicate mortgage liability_id(s): {duplicate_list}")

        property_tax_policy_by_property_month: dict[tuple[str, int], int] = {}
        for policy_index, policy in enumerate(self.property_tax_policies):
            property_purchase = purchase_by_property_id.get(policy.property_id)
            if property_purchase is None:
                known = ", ".join(repr(property_id) for property_id in sorted(purchase_by_property_id))
                raise ValueError(
                    f"property tax policy references unknown property_id {policy.property_id!r}; "
                    f"known: {known or '<none>'}"
                )
            if policy.owner_agent_id != property_purchase.buyer_agent_id:
                raise ValueError(
                    f"property tax policy for property_id {policy.property_id!r} has "
                    f"owner_agent_id={policy.owner_agent_id!r}, but the property's buyer_agent_id "
                    f"is {property_purchase.buyer_agent_id!r}"
                )
            if policy.end_month is not None and policy.end_month < policy.start_month:
                raise ValueError(
                    f"property tax policy for property_id {policy.property_id!r} has "
                    f"end_month {policy.end_month} before start_month {policy.start_month}"
                )
            for month in range(int(self.horizon_months)):
                if not policy.is_active_at(month):
                    continue
                key = (policy.property_id, month)
                previous_policy_index = property_tax_policy_by_property_month.get(key)
                if previous_policy_index is not None:
                    raise ValueError(
                        f"overlapping property tax policies for property_id {policy.property_id!r} "
                        f"at month {month}: indexes {previous_policy_index} and {policy_index}"
                    )
                property_tax_policy_by_property_month[key] = policy_index
        return self
