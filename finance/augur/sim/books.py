"""Exact observed engine books. Money and quantities retain their integer quanta."""

from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


class AccountRef(Record):
    agent_id: str
    account_id: str


class AccountBalance(Record):
    account: AccountRef
    balance: int


class IncomeState(Record):
    agent_id: str
    income_source: str
    income: int


class CapitalGainState(Record):
    agent_id: str
    short_term_gain: int
    long_term_gain: int


class SecurityLotState(Record):
    lot_id: str
    agent_id: str
    account_id: str
    asset_id: str
    purchase_month: int
    quantity_scale: int
    units_remaining: int
    basis_remaining: int


class BondState(Record):
    bond_id: str
    agent_id: str
    account_id: str
    principal: int
    active: bool


class TaxLiabilityState(Record):
    agent_id: str
    jurisdiction_id: str
    tax_year_end_month: int
    amount_owed: int
    active: bool


class PropertyState(Record):
    property_id: str
    location_id: str
    owner_agent_id: str
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

    liability_id: str
    property_id: str
    agent_id: str
    payment_account_id: str
    counterparty_agent_id: str
    counterparty_account_id: str
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

    portfolio_id: str
    owner_agent_id: str
    account_id: str
    asset_id: str
    value: int
    reported_tax_basis: int


class Book(Record):
    """Snapshot after events before `month`; a stopped book uses the result's stop mark."""

    month: int
    balances: list[AccountBalance]
    income: list[IncomeState]
    lots: list[SecurityLotState]
    bonds: list[BondState]
    properties: list[PropertyState]
    mortgages: list[MortgageState]
    tax_liabilities: list[TaxLiabilityState]
    capital_gains: list[CapitalGainState]
    tlh_portfolios: list[TlhPortfolioState]
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
    agent_id: str
    jurisdiction_id: str
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
    agent_id: str
    obligation_type: str
    amount_due: int
    amount_paid: int
    shortfall: int


class TaxSettlementOutcome(Record):
    month: int
    cause_id: str
    agent_id: str
    tax_year_end_month: int
    amount: int


class DistributionOutcome(Record):
    month: int
    agent_id: str
    holding_account_id: str
    asset_id: str
    slice_index: int
    fraction_ppb: int
    issuer_jurisdiction_id: str | None
    units: int | None
    amount: int


class BondCashflowOutcome(Record):
    month: int
    cause_id: str
    bond_id: str
    agent_id: str
    account_id: str
    issuer_jurisdiction_id: str | None
    coupon: int
    accretion: int
    redemption: int
    principal: int
