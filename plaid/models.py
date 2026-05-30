"""Typed Pydantic models for the subset of Plaid responses this repo reads.

These mirror Plaid's `/accounts/get`, `/transactions/get`, `/liabilities/get`, and
`/accounts/balance/get` payloads. Only the fields we consume are modelled; unknown
Plaid fields are ignored on validation (Pydantic's default). Parsing happens at the
client boundary so the rest of the code sees typed objects, never raw dicts. These
typed models are what the MCP tools return directly — there is no separate projection
layer.
"""

from pydantic import BaseModel, Field


class Balances(BaseModel):
    available: float | None = None
    current: float | None = None
    limit: float | None = None
    iso_currency_code: str | None = None


class Account(BaseModel):
    account_id: str
    name: str
    official_name: str | None = None
    mask: str | None = None
    type: str
    subtype: str | None = None
    balances: Balances


class PersonalFinanceCategory(BaseModel):
    primary: str | None = None
    detailed: str | None = None


class Transaction(BaseModel):
    transaction_id: str
    account_id: str
    date: str
    amount: float
    iso_currency_code: str | None = None
    name: str
    merchant_name: str | None = None
    pending: bool
    pending_transaction_id: str | None = None
    personal_finance_category: PersonalFinanceCategory | None = None


class Apr(BaseModel):
    apr_type: str
    apr_percentage: float
    balance_subject_to_apr: float | None = None
    interest_charge_amount: float | None = None


class CreditLiability(BaseModel):
    account_id: str
    aprs: list[Apr] = []
    is_overdue: bool | None = None
    last_statement_balance: float | None = None
    last_statement_issue_date: str | None = None
    minimum_payment_amount: float | None = None
    next_payment_due_date: str | None = None
    last_payment_amount: float | None = None
    last_payment_date: str | None = None


class MortgageInterestRate(BaseModel):
    percentage: float | None = None
    type: str | None = None


class Mortgage(BaseModel):
    account_id: str
    interest_rate: MortgageInterestRate | None = None
    last_payment_amount: float | None = None
    last_payment_date: str | None = None
    next_monthly_payment: float | None = None
    next_payment_due_date: str | None = None
    maturity_date: str | None = None
    origination_date: str | None = None
    origination_principal_amount: float | None = None
    past_due_amount: float | None = None
    ytd_interest_paid: float | None = None
    ytd_principal_paid: float | None = None
    loan_type_description: str | None = None


class StudentLoan(BaseModel):
    account_id: str
    loan_name: str | None = None
    interest_rate_percentage: float | None = None
    is_overdue: bool | None = None
    last_payment_amount: float | None = None
    last_payment_date: str | None = None
    last_statement_balance: float | None = None
    last_statement_issue_date: str | None = None
    minimum_payment_amount: float | None = None
    next_payment_due_date: str | None = None
    expected_payoff_date: str | None = None
    outstanding_interest_amount: float | None = None
    origination_date: str | None = None
    origination_principal_amount: float | None = None
    ytd_interest_paid: float | None = None
    ytd_principal_paid: float | None = None


class Liabilities(BaseModel):
    # Plaid sets each product array to null when the Item has no accounts of that type.
    credit: list[CreditLiability] | None = None
    mortgage: list[Mortgage] | None = None
    student: list[StudentLoan] | None = None


class AccountsGetResponse(BaseModel):
    """Response shape shared by /accounts/get and /accounts/balance/get."""

    accounts: list[Account]


class TransactionsGetResponse(BaseModel):
    accounts: list[Account]
    transactions: list[Transaction]
    # Total matching the date range before offset/count slicing (Plaid-provided).
    total_transactions: int


class TransactionPage(BaseModel):
    total: int = Field(
        description="Full count matching the date range before offset/count slicing; page until offset + count >= total."
    )
    transactions: list[Transaction]


class LiabilitiesGetResponse(BaseModel):
    accounts: list[Account]
    liabilities: Liabilities
