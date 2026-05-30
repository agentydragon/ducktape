"""Compact tool-output models and mappers from typed Plaid responses.

The Plaid client returns full typed models (`plaid.models`); these project them to
the smaller shapes the MCP tools return, so tool output stays bounded and readable.
Tools that return a plain collection return `list[...]` directly; only the
transaction page wraps its list (to carry `total` for pagination).
"""

from pydantic import BaseModel

from plaid.models import Account, CreditLiability, Transaction


class ItemSummary(BaseModel):
    key: str
    institution: str
    products: list[str]


class AccountBalances(BaseModel):
    available: float | None
    current: float | None
    limit: float | None
    iso_currency_code: str | None


class AccountOut(BaseModel):
    account_id: str
    name: str
    official_name: str | None
    mask: str | None
    type: str
    subtype: str | None
    balances: AccountBalances


class CategoryOut(BaseModel):
    primary: str | None
    detailed: str | None


class TransactionOut(BaseModel):
    transaction_id: str
    account_id: str
    date: str
    amount: float
    iso_currency_code: str | None
    name: str
    merchant_name: str | None
    category: CategoryOut | None
    pending: bool
    pending_transaction_id: str | None


class TransactionPage(BaseModel):
    # Full count matching the date range before offset/count slicing; page until
    # offset + count >= total.
    total: int
    transactions: list[TransactionOut]


class AprOut(BaseModel):
    type: str
    percentage: float
    balance_subject_to_apr: float | None
    interest_charge_amount: float | None


class CardLiabilityOut(BaseModel):
    account_id: str
    name: str
    mask: str | None
    last_statement_balance: float | None
    last_statement_issue_date: str | None
    minimum_payment_amount: float | None
    next_payment_due_date: str | None
    last_payment_amount: float | None
    last_payment_date: str | None
    is_overdue: bool | None
    aprs: list[AprOut]


def account_out(account: Account) -> AccountOut:
    b = account.balances
    return AccountOut(
        account_id=account.account_id,
        name=account.name,
        official_name=account.official_name,
        mask=account.mask,
        type=account.type,
        subtype=account.subtype,
        balances=AccountBalances(
            available=b.available, current=b.current, limit=b.limit, iso_currency_code=b.iso_currency_code
        ),
    )


def transaction_out(txn: Transaction) -> TransactionOut:
    pfc = txn.personal_finance_category
    category = CategoryOut(primary=pfc.primary, detailed=pfc.detailed) if pfc is not None else None
    return TransactionOut(
        transaction_id=txn.transaction_id,
        account_id=txn.account_id,
        date=txn.date,
        amount=txn.amount,
        iso_currency_code=txn.iso_currency_code,
        name=txn.name,
        merchant_name=txn.merchant_name,
        category=category,
        pending=txn.pending,
        pending_transaction_id=txn.pending_transaction_id,
    )


def card_liability_out(credit: CreditLiability, accounts_by_id: dict[str, Account]) -> CardLiabilityOut:
    account = accounts_by_id.get(credit.account_id)
    return CardLiabilityOut(
        account_id=credit.account_id,
        name=account.name if account is not None else credit.account_id,
        mask=account.mask if account is not None else None,
        last_statement_balance=credit.last_statement_balance,
        last_statement_issue_date=credit.last_statement_issue_date,
        minimum_payment_amount=credit.minimum_payment_amount,
        next_payment_due_date=credit.next_payment_due_date,
        last_payment_amount=credit.last_payment_amount,
        last_payment_date=credit.last_payment_date,
        is_overdue=credit.is_overdue,
        aprs=[
            AprOut(
                type=apr.apr_type,
                percentage=apr.apr_percentage,
                balance_subject_to_apr=apr.balance_subject_to_apr,
                interest_charge_amount=apr.interest_charge_amount,
            )
            for apr in credit.aprs
        ],
    )
