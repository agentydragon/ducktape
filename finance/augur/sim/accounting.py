"""Canonical cash, balanced journal, and tax effects of settled financial transactions."""

from collections.abc import Sequence
from dataclasses import dataclass

from finance.augur.sim.actions import Transfer
from finance.augur.sim.actor import Statement
from finance.augur.sim.books import (
    AccountRef,
    JournalEntry,
    Posting,
    TaxAccrual,
    TaxLiabilityState,
    TaxPaymentOutcome,
    TaxSettlementOutcome,
)
from finance.augur.sim.compiler.income_sources import income_source_wire_id
from finance.augur.sim.compiler.tax import PreparedTaxProfile
from finance.augur.sim.ledger import Ledger
from finance.augur.sim.money import checked_count
from finance.augur.sim.mortgage import Mortgage
from finance.augur.sim.prepared import PreparedAccount, PreparedJurisdiction
from finance.augur.sim.scenario import TransferDeductionCategory, TransferIncomeCategory
from finance.augur.sim.tax_year import TaxBook


@dataclass(frozen=True)
class TransferOutcome:
    month: int
    cause_id: str
    from_account: AccountRef
    to_account: AccountRef
    amount: int
    income_category: str | None


@dataclass(frozen=True)
class MortgagePaymentOutcome:
    month: int
    cause_id: str
    liability_id: str
    agent_id: str
    counterparty_agent_id: str
    property_id: str
    from_account_id: str
    to_account_id: str
    interest: int
    principal: int
    total_payment: int


class AccountStatement(Statement):
    """The owner's declared accounts and their balances, in declaration order."""

    accounts: tuple[tuple[str, int], ...]


class TaxLiabilityStatement(Statement):
    """The tax book's assessed liabilities, for the authority that collects them."""

    liabilities: tuple[TaxLiabilityState, ...]


class Accounting:
    """Ledger, tax book and outstanding liabilities are state; the outcome lists hold only this month.

    `journal`, `transfers`, `tax_accruals`, `tax_payments`, `tax_settlements` and
    `mortgage_payments` are cleared by `begin_month`; a caller wanting a history copies
    them between months.
    """

    def __init__(
        self, income_sources: Sequence[TransferIncomeCategory], jurisdictions: Sequence[PreparedJurisdiction]
    ) -> None:
        self.declared: tuple[AccountRef, ...] = ()
        self.ledger = Ledger(())
        self.tax = TaxBook(income_sources, jurisdictions)
        self.journal: list[JournalEntry] = []
        self.transfers: list[TransferOutcome] = []
        self.tax_accruals: list[TaxAccrual] = []
        self.tax_liabilities: list[TaxLiabilityState] = []
        self.tax_payments: list[TaxPaymentOutcome] = []
        self.tax_settlements: list[TaxSettlementOutcome] = []
        self.mortgage_payments: list[MortgagePaymentOutcome] = []
        self.ledger.ensure_account(AccountRef(agent_id="__external__", account_id="boundary"))

    def declare(self, account: PreparedAccount) -> None:
        """Open a household-facing account with its month-zero balance against the owner's opening equity."""
        if account.account in self.declared:
            raise ValueError(f"account {account.account!r} is already declared")
        self.declared = (*self.declared, account.account)
        self.ledger.ensure_account(account.account)
        equity = AccountRef(agent_id=account.account.agent_id, account_id="equity:opening")
        self.ledger.ensure_account(equity)
        if account.opening_balance:
            self.apply(
                JournalEntry(
                    month=0,
                    cause_id=f"opening:{account.account.agent_id}:{account.account.account_id}",
                    postings=[
                        Posting(account=account.account, amount=account.opening_balance),
                        Posting(account=equity, amount=checked_count(-account.opening_balance, "money negation")),
                    ],
                )
            )

    def enroll(self, profile: PreparedTaxProfile) -> None:
        """Take on a taxpayer: its year state, prepayment asset and the accounts its assessments post to."""
        self.tax.enroll(profile)
        self.ledger.ensure_account(AccountRef(agent_id=profile.agent_id, account_id="asset:tax-prepayments"))
        self.ledger.ensure_account(
            AccountRef(agent_id=profile.tax_authority_agent_id, account_id="income:tax-payments")
        )
        for rules in profile.jurisdictions:
            for kind in ("expense", "liability"):
                self.ledger.ensure_account(
                    AccountRef(agent_id=profile.agent_id, account_id=f"{kind}:tax:{rules.jurisdiction_id}")
                )

    def statement(self, actor: str, month: int) -> AccountStatement:
        return AccountStatement(
            month=month,
            accounts=tuple(
                (account.account_id, self.ledger.balance(account))
                for account in self.declared
                if account.agent_id == actor
            ),
        )

    def liability_statement(self, month: int) -> TaxLiabilityStatement:
        return TaxLiabilityStatement(month=month, liabilities=tuple(self.tax_liabilities))

    def begin_month(self) -> None:
        self.journal.clear()
        self.transfers.clear()
        self.tax_accruals.clear()
        self.tax_payments.clear()
        self.tax_settlements.clear()
        self.mortgage_payments.clear()

    def apply(self, entry: JournalEntry) -> None:
        self.ledger.apply(entry)
        self.journal.append(entry)

    def apply_entries(self, entries: Sequence[JournalEntry]) -> None:
        self.ledger.apply_all(entries)
        self.journal.extend(entries)

    def move(self, month: int, cause_id: str, source: AccountRef, destination: AccountRef, amount: int) -> None:
        self.apply(
            JournalEntry(
                month=month,
                cause_id=cause_id,
                postings=[
                    Posting(account=source, amount=checked_count(-amount, "money negation")),
                    Posting(account=destination, amount=amount),
                ],
            )
        )

    def transfer(
        self,
        month: int,
        request: Transfer,
        *,
        actor: str | None,
        income: TransferIncomeCategory | None = None,
        deduction: TransferDeductionCategory | None = None,
    ) -> None:
        if actor is not None:
            if not request.cause_id:
                raise ValueError("empty transfer request identifier")
            if request.from_account.agent_id != actor:
                raise ValueError("source account does not belong to the actor")
            if income is not None or deduction is not None:
                raise ValueError("bare actor transfers cannot declare tax character")
        if request.from_account not in self.declared or request.to_account not in self.declared:
            raise ValueError("unknown declared account")
        if actor is not None and (request.amount <= 0 or request.amount > self.ledger.balance(request.from_account)):
            raise ValueError("amount must be positive and covered by available cash")
        if deduction is not None and deduction != "ordinary":
            raise ValueError("unsupported deduction category")
        candidate = self.tax.income.copy()
        if income is not None:
            candidate.accrue(request.to_account.agent_id, income, request.amount)
        if deduction == "ordinary":
            candidate.deduct_from_ordinary(request.from_account.agent_id, request.amount)
        self.move(month, request.cause_id, request.from_account, request.to_account, request.amount)
        self.tax.income = candidate
        self.transfers.append(
            TransferOutcome(
                month,
                request.cause_id,
                request.from_account,
                request.to_account,
                request.amount,
                income_source_wire_id(income)
                if income is not None and request.to_account.agent_id in self.tax.years
                else None,
            )
        )

    def close_tax_year(self, month: int, mortgages: Sequence[Mortgage]) -> None:
        assessments = self.tax.assessments(month, mortgages)
        # One group, so a bad jurisdiction does not commit the jurisdictions assessed before it.
        self.apply_entries(
            [
                JournalEntry(
                    month=month,
                    cause_id=row.cause_id,
                    postings=[
                        Posting(
                            account=AccountRef(agent_id=row.agent_id, account_id=f"expense:tax:{row.jurisdiction_id}"),
                            amount=row.total_tax,
                        ),
                        Posting(
                            account=AccountRef(
                                agent_id=row.agent_id, account_id=f"liability:tax:{row.jurisdiction_id}"
                            ),
                            amount=checked_count(-row.total_tax, "money negation"),
                        ),
                    ],
                )
                for row in assessments
                if row.total_tax
            ]
        )
        self.tax_accruals.extend(assessments)
        self.tax_liabilities.extend(
            TaxLiabilityState(
                agent_id=row.agent_id,
                jurisdiction_id=row.jurisdiction_id,
                tax_year_end_month=month,
                amount_owed=row.total_tax,
                active=True,
            )
            for row in assessments
        )
        self.tax.reset(assessments)
