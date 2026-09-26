"""Exact observed engine books. Money and quantities retain their integer quanta."""

from pydantic import BaseModel, ConfigDict

from finance.augur.model.series import LocationId
from finance.augur.sim.ids import (
    AccountId,
    AgentId,
    AssetId,
    BondId,
    JurisdictionId,
    LiabilityId,
    LotId,
    PortfolioId,
    PropertyId,
)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


class AccountRef(Record):
    agent_id: AgentId
    account_id: AccountId


# The contra account for money entering or leaving the modeled books.
EXTERNAL_BOUNDARY = AccountRef(agent_id=AgentId("__external__"), account_id=AccountId("boundary"))


class AccountBalance(Record):
    account: AccountRef
    balance: int


class IncomeState(Record):
    agent_id: AgentId
    income_source: str
    income: int


class CapitalGainState(Record):
    agent_id: AgentId
    short_term_gain: int
    long_term_gain: int


class SecurityLotState(Record):
    lot_id: LotId
    agent_id: AgentId
    account_id: AccountId
    # The asset's `AssetKey` wire id (`parse_asset_key`), not the sim's `AssetId`.
    asset_id: str
    purchase_month: int
    quantity_scale: int
    units_remaining: int
    basis_remaining: int


class BondState(Record):
    bond_id: BondId
    agent_id: AgentId
    account_id: AccountId
    principal: int
    active: bool


class TaxLiabilityState(Record):
    agent_id: AgentId
    jurisdiction_id: JurisdictionId
    tax_year_end_month: int
    amount_owed: int
    active: bool


class PropertyState(Record):
    property_id: PropertyId
    location_id: LocationId
    owner_agent_id: AgentId
    purchase_month: int
    adjusted_basis: int
    rented_fraction_ppb: int
    building_basis_initial: int
    building_basis: int
    cumulative_depreciation: int
    depreciation_ytd: int
    owner_occupied_months: int
    contribution_used: int
    equity_ledger: int
    active: bool


class MortgageState(Record):
    """Read-only capture combining the Python contract's servicing facts and ledger principal."""

    liability_id: LiabilityId
    property_id: PropertyId
    agent_id: AgentId
    payment_account_id: AccountId
    counterparty_agent_id: AgentId
    counterparty_account_id: AccountId
    origination_month: int
    annual_interest_rate_ppb: int
    term_months: int
    monthly_payment: int
    principal: int
    interest_paid_ytd: int
    rental_interest_paid_ytd: int
    active: bool


class TlhPortfolioState(Record):
    """Reported portfolio facts, never a mirror of its private cohorts."""

    portfolio_id: PortfolioId
    owner_agent_id: AgentId
    account_id: AccountId
    asset_id: AssetId
    value: int
    reported_tax_basis: int


class Book(Record):
    """Snapshot after events before `month`; a stopped book uses the result's stop mark.

    A domain the world does not have is `None`, not empty: `bonds` until a bond is held,
    `properties` until housing is declared, `tlh_portfolios` until a portfolio is declared.
    """

    month: int
    balances: list[AccountBalance]
    income: list[IncomeState]
    lots: list[SecurityLotState]
    bonds: list[BondState] | None = None
    properties: list[PropertyState] | None = None
    mortgages: list[MortgageState]
    tax_liabilities: list[TaxLiabilityState]
    capital_gains: list[CapitalGainState]
    tlh_portfolios: list[TlhPortfolioState] | None = None
    failed: bool


class Posting(Record):
    account: AccountRef
    amount: int


class JournalEntry(Record):
    month: int
    cause_id: str
    postings: list[Posting]


class TaxAccrual(Record):
    month: int
    cause_id: str
    agent_id: AgentId
    jurisdiction_id: JurisdictionId
    tax_year_end_month: int
    ordinary_income: int
    short_term_gain: int
    long_term_gain: int
    section_1250_recapture: int
    rental_interest_deduction: int
    depreciation_deduction: int
    standard_deduction: int
    mortgage_interest_deduction: int
    salt_deduction: int
    itemized_deduction: int
    ordinary_taxable: int
    long_term_capital_gain_taxable: int
    ordinary_tax: int
    capital_gain_tax: int
    section_1250_tax: int
    total_tax: int
    capital_loss_carryforward: int


class TaxPaymentOutcome(Record):
    month: int
    cause_id: str
    agent_id: AgentId
    obligation_type: str
    amount_due: int
    amount_paid: int
    shortfall: int


class TaxSettlementOutcome(Record):
    month: int
    cause_id: str
    agent_id: AgentId
    tax_year_end_month: int
    amount: int


class DistributionOutcome(Record):
    month: int
    agent_id: AgentId
    holding_account_id: AccountId
    asset_id: AssetId
    slice_index: int
    fraction_ppb: int
    issuer_jurisdiction_id: JurisdictionId | None
    units: int | None
    amount: int


class BondCashflowOutcome(Record):
    month: int
    cause_id: str
    bond_id: BondId
    agent_id: AgentId
    account_id: AccountId
    issuer_jurisdiction_id: JurisdictionId | None
    coupon: int
    accretion: int
    redemption: int
    principal: int
