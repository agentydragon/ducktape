"""Pydantic models for the augur frontend's nested URL/browser scenario state.

These shapes are the source of truth for the UI-organized layout the React app
maintains and round-trips through the URL. The wire-facing
``augur.core.scenario_set.ScenarioSet`` is a *different* shape: flat lists of
actors, events, and policies derived from this nested input by the frontend's
``scenarioSetInputToRequest`` mapper. Both shapes need a generated Zod schema;
the wire shape's came along for free with the SSOT pipeline, this module
exists so the nested input gets the same treatment instead of being
hand-maintained in JS via ``SCENARIO_INPUT_SECTION_FIELDS`` /
``FINANCING_MODE_IDS`` / ``PRIVATE_EQUITY_SALE_POLICY_IDS`` constants.

Two flavors per section: the strict shape (every field required) describes
the *normalized* layout the React app operates on after merging URL state
with bootstrap defaults; the ``…Overrides`` mirror (every field optional)
describes the sparse-overrides payload the URL actually stores, which lets
``decodeScenarioSetUrlState`` validate URL state at the Zod boundary.

URL state version (``URL_STATE_VERSION`` in ``scenario_set_state.js``) is the
contract version for the encoded form. Bumping any field here is a wire-state
break and demands a version bump.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict

from augur.core.bootstrap import ActorPolicyId, LiquidReservePolicyId, OwnerResidenceModeId, RentalUsePolicyId
from augur.core.scenario_set import FinancingMode, MarketRequest, ReportSpec
from augur.core.schemas import ApiModel

# ---------------------------------------------------------------------------
# Browser scenario set input — strict shape. Every field present after
# normalizeScenarioSetInput materializes from URL state + bootstrap defaults.
# ---------------------------------------------------------------------------


class BrowserScenarioIdentity(ApiModel):
    scenario_id: str
    label: str
    enabled: bool
    color: str


class BrowserPropertyAndLocation(ApiModel):
    property_id: str


class BrowserActorsAndOwnership(ApiModel):
    actor_policy: ActorPolicyId
    partner_payment_monthly_usd: float


class BrowserTimeline(ApiModel):
    hold_years: float


class BrowserFinancing(ApiModel):
    financing_mode: FinancingMode
    down_payment_pct: float
    custom_mortgage_rate: float | None = None
    custom_mortgage_term_years: float | None = None
    credit_score: float | None = None


class BrowserOccupancyAndRental(ApiModel):
    owner_residence_mode: OwnerResidenceModeId
    rental_use_policy: RentalUsePolicyId
    vacancy_pct: float
    management_fee_pct: float
    leasing_fee_pct: float
    rooms_rented_while_living: float
    room_rent_monthly_usd: float
    room_vacancy_pct: float


class BrowserPropertyAssumptions(ApiModel):
    maintenance_pct: float
    insurance_annual_usd: float
    depreciable_basis_pct: float


class BrowserTaxAccounting(ApiModel):
    closing_cost_buy_pct: float
    closing_cost_sell_pct: float


class BrowserInitialBalanceSheet(ApiModel):
    initial_checking_usd: float
    starting_portfolio_usd: float
    private_equity_units: float


# Browser-side switch for whether the private-equity sale policy is enabled,
# and which sale rule shape applies. The backend's analogous Pydantic models
# (``PrivateEquitySalePolicy`` and its ``PrivateEquitySaleRule`` discriminated
# union) describe the policy *instance* the simulator runs — not the
# off/on/which-rule selector this enum drives.
class PrivateEquitySalePolicyId(StrEnum):
    NONE = "none"
    LIQUID_NET_WORTH_FLOOR = "liquid_net_worth_floor"


class BrowserPolicies(ApiModel):
    liquid_reserve_policy: LiquidReservePolicyId
    checking_floor_usd: float
    checking_sale_amount_usd: float
    private_equity_sale_policy: PrivateEquitySalePolicyId
    private_equity_liquid_net_worth_floor_usd: float
    private_equity_tender_sale_amount_usd: float


class BrowserScenarioInput(ApiModel):
    identity: BrowserScenarioIdentity
    property_and_location: BrowserPropertyAndLocation
    actors_and_ownership: BrowserActorsAndOwnership
    timeline: BrowserTimeline
    financing: BrowserFinancing
    occupancy_and_rental: BrowserOccupancyAndRental
    property_assumptions: BrowserPropertyAssumptions
    tax_accounting: BrowserTaxAccounting
    initial_balance_sheet: BrowserInitialBalanceSheet
    policies: BrowserPolicies


class BrowserScenarioSetInput(ApiModel):
    title: str
    market_request: MarketRequest
    report_spec: ReportSpec
    scenarios: tuple[BrowserScenarioInput, ...]


# ---------------------------------------------------------------------------
# Browser scenario set input — sparse-overrides shape. What
# ``encodeScenarioSetUrlState`` actually writes: only the fields the user
# changed away from bootstrap defaults persist; everything else stays absent
# and gets re-derived at decode time. Every field is optional at every depth.
# Unknown keys are silently ignored so old URLs from before a field was added
# don't blow up newer code.
# ---------------------------------------------------------------------------


class _Overrides(ApiModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class BrowserScenarioIdentityOverrides(_Overrides):
    scenario_id: str | None = None
    label: str | None = None
    enabled: bool | None = None
    color: str | None = None


class BrowserPropertyAndLocationOverrides(_Overrides):
    property_id: str | None = None


class BrowserActorsAndOwnershipOverrides(_Overrides):
    actor_policy: ActorPolicyId | None = None
    partner_payment_monthly_usd: float | None = None


class BrowserTimelineOverrides(_Overrides):
    hold_years: float | None = None


class BrowserFinancingOverrides(_Overrides):
    financing_mode: FinancingMode | None = None
    down_payment_pct: float | None = None
    custom_mortgage_rate: float | None = None
    custom_mortgage_term_years: float | None = None
    credit_score: float | None = None


class BrowserOccupancyAndRentalOverrides(_Overrides):
    owner_residence_mode: OwnerResidenceModeId | None = None
    rental_use_policy: RentalUsePolicyId | None = None
    vacancy_pct: float | None = None
    management_fee_pct: float | None = None
    leasing_fee_pct: float | None = None
    rooms_rented_while_living: float | None = None
    room_rent_monthly_usd: float | None = None
    room_vacancy_pct: float | None = None


class BrowserPropertyAssumptionsOverrides(_Overrides):
    maintenance_pct: float | None = None
    insurance_annual_usd: float | None = None
    depreciable_basis_pct: float | None = None


class BrowserTaxAccountingOverrides(_Overrides):
    closing_cost_buy_pct: float | None = None
    closing_cost_sell_pct: float | None = None


class BrowserInitialBalanceSheetOverrides(_Overrides):
    initial_checking_usd: float | None = None
    starting_portfolio_usd: float | None = None
    private_equity_units: float | None = None


class BrowserPoliciesOverrides(_Overrides):
    liquid_reserve_policy: LiquidReservePolicyId | None = None
    checking_floor_usd: float | None = None
    checking_sale_amount_usd: float | None = None
    private_equity_sale_policy: PrivateEquitySalePolicyId | None = None
    private_equity_liquid_net_worth_floor_usd: float | None = None
    private_equity_tender_sale_amount_usd: float | None = None


class BrowserScenarioInputOverrides(_Overrides):
    identity: BrowserScenarioIdentityOverrides | None = None
    property_and_location: BrowserPropertyAndLocationOverrides | None = None
    actors_and_ownership: BrowserActorsAndOwnershipOverrides | None = None
    timeline: BrowserTimelineOverrides | None = None
    financing: BrowserFinancingOverrides | None = None
    occupancy_and_rental: BrowserOccupancyAndRentalOverrides | None = None
    property_assumptions: BrowserPropertyAssumptionsOverrides | None = None
    tax_accounting: BrowserTaxAccountingOverrides | None = None
    initial_balance_sheet: BrowserInitialBalanceSheetOverrides | None = None
    policies: BrowserPoliciesOverrides | None = None


class BrowserScenarioSetInputOverrides(_Overrides):
    title: str | None = None
    market_request: MarketRequest | None = None
    report_spec: ReportSpec | None = None
    scenarios: tuple[BrowserScenarioInputOverrides, ...] | None = None
