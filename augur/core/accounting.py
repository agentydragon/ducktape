from __future__ import annotations

from collections import defaultdict
from enum import StrEnum

from pydantic import Field, NonNegativeFloat, NonNegativeInt, model_validator

from augur.core.schemas import ApiModel


class ChartAccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"


class ChartAccountRole(StrEnum):
    CHECKING_CASH = "checking_cash"
    PUBLIC_SECURITY = "public_security"
    CRYPTO_ASSET = "crypto_asset"
    PRIVATE_EQUITY = "private_equity"
    PROPERTY = "property"
    MORTGAGE_PAYABLE = "mortgage_payable"
    TAX_PAYABLE = "tax_payable"
    PARTNER_CLAIM = "partner_claim"
    OWNER_EQUITY_CLAIM = "owner_equity_claim"
    OPENING_EQUITY = "opening_equity"
    PARTNER_EQUITY_LEDGER = "partner_equity_ledger"
    OWNER_EQUITY_LEDGER = "owner_equity_ledger"
    PARTNER_HOME_EQUITY_CLAIM = "partner_home_equity_claim"
    OWNER_HOME_EQUITY_CLAIM = "owner_home_equity_claim"
    MONTHLY_LIVING_EXPENSE = "monthly_living_expense"
    MORTGAGE_INTEREST_EXPENSE = "mortgage_interest_expense"
    PROPERTY_TAX_EXPENSE = "property_tax_expense"
    PROPERTY_PURCHASE_CLOSING_EXPENSE = "property_purchase_closing_expense"
    HOA_EXPENSE = "hoa_expense"
    INSURANCE_EXPENSE = "insurance_expense"
    MAINTENANCE_EXPENSE = "maintenance_expense"
    RENTAL_INCOME = "rental_income"
    RENTAL_MANAGEMENT_FEE_EXPENSE = "rental_management_fee_expense"
    RENTAL_LEASING_FEE_EXPENSE = "rental_leasing_fee_expense"
    PROPERTY_SALE_PROCEEDS = "property_sale_proceeds"
    PROPERTY_SALE_CLOSING_EXPENSE = "property_sale_closing_expense"
    REALIZED_CAPITAL_GAIN = "realized_capital_gain"
    TAX_EXPENSE = "tax_expense"
    PARTNER_CONTRIBUTION_TRANSFER = "partner_contribution_transfer"
    PARTNER_CONTRIBUTION_USED = "partner_contribution_used"
    PARTNER_UNALLOCATED_CLAIM = "partner_unallocated_claim"
    PARTNER_PRINCIPAL_CREDIT = "partner_principal_credit"
    OWNER_PRINCIPAL_CREDIT = "owner_principal_credit"
    PRINCIPAL_CREDIT_ALLOCATION = "principal_credit_allocation"


class PostingSide(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class JournalEntryType(StrEnum):
    OPENING_BALANCE = "opening_balance"
    CASH_EXPENSE = "cash_expense"
    MORTGAGE_PAYMENT = "mortgage_payment"
    PROPERTY_OPERATING = "property_operating"
    ASSET_SALE = "asset_sale"
    PROPERTY_SALE = "property_sale"
    TAX_ACCRUAL = "tax_accrual"
    OBLIGATION_SETTLEMENT = "obligation_settlement"
    PARTNER_CONTRIBUTION = "partner_contribution"
    OWNERSHIP_CLAIM_ACCRUAL = "ownership_claim_accrual"


class AccountingCauseType(StrEnum):
    OPENING_BALANCE = "opening_balance"
    POLICY_DECISION = "policy_decision"
    SCHEDULED_EVENT = "scheduled_event"
    MARKET_OBSERVATION = "market_observation"
    ACCOUNTING_PROCESS = "accounting_process"
    OBLIGATION_SETTLEMENT = "obligation_settlement"


class LotAssetClass(StrEnum):
    PUBLIC_SECURITY = "public_security"
    CRYPTO = "crypto"
    PRIVATE_EQUITY = "private_equity"
    PROPERTY = "property"


class LiabilityType(StrEnum):
    MORTGAGE = "mortgage"
    TAX_PAYABLE = "tax_payable"
    PARTNER_CLAIM = "partner_claim"
    CREDIT_FACILITY = "credit_facility"


class ChartAccount(ApiModel):
    chart_account_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-:.]*$")
    account_type: ChartAccountType
    role: ChartAccountRole
    actor_id: str | None = None
    label: str | None = None
    source_account_id: str | None = None
    source_asset_id: str | None = None
    liability_id: str | None = None
    property_id: str | None = None
    counterparty_actor_id: str | None = None


class AccountingCause(ApiModel):
    cause_type: AccountingCauseType
    cause_id: str
    policy_id: str | None = None
    event_id: str | None = None
    obligation_id: str | None = None
    market_observation_id: str | None = None


class JournalEntry(ApiModel):
    journal_entry_id: str
    rollout_index: NonNegativeInt
    month_index: NonNegativeInt
    journal_entry_type: JournalEntryType
    cause: AccountingCause
    actor_id: str | None = None
    policy_id: str | None = None
    event_id: str | None = None
    obligation_id: str | None = None
    description: str | None = None
    path_set_id: str | None = None
    exogenous_path_id: str | None = None
    scenario_input_id: str | None = None
    projection_trajectory_id: str | None = None


class Posting(ApiModel):
    posting_id: str
    journal_entry_id: str
    rollout_index: NonNegativeInt
    month_index: NonNegativeInt
    chart_account_id: str
    side: PostingSide
    amount_usd: NonNegativeFloat
    quantity: NonNegativeFloat | None = None
    lot_id: str | None = None
    liability_id: str | None = None
    path_set_id: str | None = None
    exogenous_path_id: str | None = None
    scenario_input_id: str | None = None
    projection_trajectory_id: str | None = None

    @model_validator(mode="after")
    def _positive_amount(self) -> Posting:
        if self.amount_usd <= 0:
            raise ValueError("posting amount_usd must be positive")
        return self


class BalanceSnapshot(ApiModel):
    rollout_index: NonNegativeInt
    month_index: NonNegativeInt
    chart_account_id: str
    balance_usd: float
    quantity: NonNegativeFloat | None = None
    path_set_id: str | None = None
    exogenous_path_id: str | None = None
    scenario_input_id: str | None = None
    projection_trajectory_id: str | None = None


class TaxLot(ApiModel):
    lot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-:.]*$")
    asset_class: LotAssetClass
    owner_actor_id: str
    source_account_id: str | None = None
    source_asset_id: str | None = None
    property_id: str | None = None
    quantity: NonNegativeFloat | None = None
    cost_basis_usd: NonNegativeFloat
    acquisition_month_index: NonNegativeInt = 0


class LotDisposition(ApiModel):
    lot_disposition_id: str
    journal_entry_id: str
    rollout_index: NonNegativeInt
    month_index: NonNegativeInt
    lot_id: str
    asset_class: LotAssetClass
    proceeds_usd: NonNegativeFloat
    cost_basis_usd: NonNegativeFloat
    realized_gain_usd: float
    taxable_gain_usd: float
    quantity_sold: NonNegativeFloat | None = None
    tax_expense_usd: NonNegativeFloat = 0.0
    path_set_id: str | None = None
    exogenous_path_id: str | None = None
    scenario_input_id: str | None = None
    projection_trajectory_id: str | None = None


class LiabilityState(ApiModel):
    liability_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-:.]*$")
    liability_type: LiabilityType
    actor_id: str
    creditor_id: str | None = None
    counterparty_actor_id: str | None = None
    property_id: str | None = None
    balance_usd: float


class AccountingValidationError(ValueError):
    pass


def debit_amount(posting: Posting) -> float:
    return posting.amount_usd if posting.side is PostingSide.DEBIT else 0.0


def credit_amount(posting: Posting) -> float:
    return posting.amount_usd if posting.side is PostingSide.CREDIT else 0.0


def signed_balance_delta(posting: Posting, account: ChartAccount) -> float:
    normal_debit = account.account_type in {ChartAccountType.ASSET, ChartAccountType.EXPENSE}
    if posting.side is PostingSide.DEBIT:
        return posting.amount_usd if normal_debit else -posting.amount_usd
    return -posting.amount_usd if normal_debit else posting.amount_usd


def validate_accounting_trace(
    *,
    chart_accounts: tuple[ChartAccount, ...],
    journal_entries: tuple[JournalEntry, ...],
    postings: tuple[Posting, ...],
    tolerance_usd: float = 0.005,
) -> None:
    account_ids = [account.chart_account_id for account in chart_accounts]
    duplicate_accounts = sorted(_repeated(account_ids))
    if duplicate_accounts:
        raise AccountingValidationError(f"duplicate chart account ids: {duplicate_accounts}")
    account_by_id = {account.chart_account_id: account for account in chart_accounts}

    journal_ids = [entry.journal_entry_id for entry in journal_entries]
    duplicate_journals = sorted(_repeated(journal_ids))
    if duplicate_journals:
        raise AccountingValidationError(f"duplicate journal entry ids: {duplicate_journals}")
    journal_by_id = {entry.journal_entry_id: entry for entry in journal_entries}

    postings_by_journal: dict[str, list[Posting]] = defaultdict(list)
    posting_ids: list[str] = []
    for posting in postings:
        posting_ids.append(posting.posting_id)
        if posting.journal_entry_id not in journal_by_id:
            raise AccountingValidationError(f"posting {posting.posting_id} references unknown journal entry")
        if posting.chart_account_id not in account_by_id:
            raise AccountingValidationError(f"posting {posting.posting_id} references unknown chart account")
        postings_by_journal[posting.journal_entry_id].append(posting)

    duplicate_postings = sorted(_repeated(posting_ids))
    if duplicate_postings:
        raise AccountingValidationError(f"duplicate posting ids: {duplicate_postings}")

    for journal_id, entry in journal_by_id.items():
        journal_postings = postings_by_journal.get(journal_id, [])
        if not journal_postings:
            raise AccountingValidationError(f"journal entry {journal_id} has no postings")
        debits = sum(debit_amount(posting) for posting in journal_postings)
        credits = sum(credit_amount(posting) for posting in journal_postings)
        if abs(debits - credits) > tolerance_usd:
            raise AccountingValidationError(
                f"journal entry {entry.journal_entry_id} is unbalanced: debits={debits} credits={credits}"
            )


def chart_account_id(
    role: ChartAccountRole,
    *,
    actor_id: str | None = None,
    source_account_id: str | None = None,
    source_asset_id: str | None = None,
    liability_id: str | None = None,
    property_id: str | None = None,
    counterparty_actor_id: str | None = None,
) -> str:
    parts = [role.value]
    if actor_id is not None:
        parts.append(f"actor:{actor_id}")
    if source_account_id is not None:
        parts.append(f"account:{source_account_id}")
    if source_asset_id is not None:
        parts.append(f"asset:{source_asset_id}")
    if liability_id is not None:
        parts.append(f"liability:{liability_id}")
    if property_id is not None:
        parts.append(f"property:{property_id}")
    if counterparty_actor_id is not None:
        parts.append(f"counterparty:{counterparty_actor_id}")
    return ":".join(parts)


def chart_account_type_for_role(role: ChartAccountRole) -> ChartAccountType:
    match role:
        case (
            ChartAccountRole.CHECKING_CASH
            | ChartAccountRole.PUBLIC_SECURITY
            | ChartAccountRole.CRYPTO_ASSET
            | ChartAccountRole.PRIVATE_EQUITY
            | ChartAccountRole.PROPERTY
            | ChartAccountRole.PARTNER_CLAIM
            | ChartAccountRole.OWNER_EQUITY_CLAIM
            | ChartAccountRole.PARTNER_EQUITY_LEDGER
            | ChartAccountRole.OWNER_EQUITY_LEDGER
            | ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM
            | ChartAccountRole.OWNER_HOME_EQUITY_CLAIM
            | ChartAccountRole.PARTNER_UNALLOCATED_CLAIM
            | ChartAccountRole.PARTNER_PRINCIPAL_CREDIT
            | ChartAccountRole.OWNER_PRINCIPAL_CREDIT
        ):
            return ChartAccountType.ASSET
        case ChartAccountRole.MORTGAGE_PAYABLE | ChartAccountRole.TAX_PAYABLE:
            return ChartAccountType.LIABILITY
        case ChartAccountRole.OPENING_EQUITY | ChartAccountRole.PRINCIPAL_CREDIT_ALLOCATION:
            return ChartAccountType.EQUITY
        case (
            ChartAccountRole.RENTAL_INCOME
            | ChartAccountRole.PROPERTY_SALE_PROCEEDS
            | ChartAccountRole.REALIZED_CAPITAL_GAIN
            | ChartAccountRole.PARTNER_CONTRIBUTION_TRANSFER
        ):
            return ChartAccountType.INCOME
        case _:
            return ChartAccountType.EXPENSE


def _repeated(values: list[str]) -> set[str]:
    return {value for value in values if values.count(value) > 1}
