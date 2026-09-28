"""Owned, resolved simulation facts: exact money/quantity counts.

The location a housing declaration takes, and the underscored configured records the housing,
tender and deduction declarations take until their policies move to the common action session.
"""

from dataclasses import dataclass
from typing import Literal

from finance.augur.model.series import LocationId
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, LiabilityId, PropertyId
from finance.augur.sim.market_path import Amount


@dataclass(frozen=True, kw_only=True)
class PreparedLocation:
    location_id: LocationId
    display_name: str
    jurisdiction_ids: tuple[JurisdictionId, ...]
    annual_property_tax_rate_ppb: int
    annual_special_assessment: int


@dataclass(frozen=True, kw_only=True)
class _TenderPolicy:
    owner_agent_id: AgentId
    proceeds_account_id: AccountId
    liquid_net_worth_floor: Amount


@dataclass(frozen=True, kw_only=True)
class _MortgageFinancing:
    liability_id: LiabilityId
    lender_agent_id: AgentId
    lender_account_id: AccountId
    principal: int
    annual_interest_rate_ppb: int
    term_months: int


@dataclass(frozen=True, kw_only=True)
class _PropertyPurchase:
    month: int
    cause_id: str
    property_id: PropertyId
    location_id: LocationId
    buyer_agent_id: AgentId
    buyer_account_id: AccountId
    seller_agent_id: AgentId
    seller_account_id: AccountId
    purchase_price: int
    down_payment: int
    buyer_closing_cost: int
    rented_fraction_ppb: int
    land_value_fraction_ppb: int
    mortgage: _MortgageFinancing | None


@dataclass(frozen=True, kw_only=True)
class _PrimaryResidence:
    agent_id: AgentId
    property_id: PropertyId


@dataclass(frozen=True, kw_only=True)
class _PrimaryResidenceEvent:
    month: int
    agent_id: AgentId
    property_id: PropertyId | None


@dataclass(frozen=True, kw_only=True)
class _RentedFraction:
    month: int
    property_id: PropertyId
    rented_fraction_ppb: int


@dataclass(frozen=True, kw_only=True)
class _CapitalImprovement:
    month: int
    property_id: PropertyId
    amount: int
    description: str


@dataclass(frozen=True, kw_only=True)
class _PropertySale:
    month: int
    property_id: PropertyId
    closing_cost_ppb: int


@dataclass(frozen=True, kw_only=True)
class _MortgageInterestDeduction:
    liability_id: LiabilityId
    owner_agent_id: AgentId
    debt_class: Literal["acquisition", "home_equity"]
    per_jurisdiction_principal_cap: dict[JurisdictionId, int]


@dataclass(frozen=True, kw_only=True)
class _PropertyTax:
    property_id: PropertyId
    owner_agent_id: AgentId
    from_account_id: AccountId
    tax_authority_agent_id: AgentId
    tax_authority_account_id: AccountId
    annual_tax_rate_ppb: int | None
    start_month: int
    end_month: int | None


@dataclass(frozen=True, kw_only=True)
class _SaltCap:
    effective_year_index: int
    cap: int


@dataclass(frozen=True, kw_only=True)
class _SaltDeduction:
    """Federal SALT itemized deduction (Schedule A) for one enrolled taxpayer.

    Each of the profile's jurisdictions other than `federal_jurisdiction_id` contributes the
    income tax it accrued this calendar year, alongside property tax paid; the total is capped
    by the latest `cap_schedule` entry in effect (year index 0-based from the horizon's start;
    an empty schedule is uncapped) and stacks with mortgage interest. Not modeled: the AGI
    phase-out of the cap, the sales-tax election, and deducting prior-year true-ups in the year
    they are paid.
    """

    profile_id: AgentId
    federal_jurisdiction_id: JurisdictionId
    cap_schedule: tuple[_SaltCap, ...]
