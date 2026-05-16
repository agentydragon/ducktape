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

URL state version (``URL_STATE_VERSION`` in ``scenario_set_state.js``) is the
contract version for the encoded form. Bumping any field here is a wire-state
break and demands a version bump.
"""

from __future__ import annotations

from enum import StrEnum

from augur.core.bootstrap import ActorPolicyId, LiquidReservePolicyId, OwnerResidenceModeId, RentalUsePolicyId
from augur.core.scenario_set import FinancingMode, MarketRequest, ReportSpec
from augur.core.schemas import ApiModel


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
