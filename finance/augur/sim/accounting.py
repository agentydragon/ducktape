"""Canonical cash, balanced journal, and tax effects of settled financial transactions."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from finance.augur.sim.actions import Transfer
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
from finance.augur.sim.prepared import PreparedAccount, PreparedScenario
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


class Accounting:
    def __init__(
        self,
        accounts: Sequence[PreparedAccount],
        profiles: Sequence[PreparedTaxProfile],
        income_sources: Sequence[TransferIncomeCategory],
        *,
        capture: Literal["summary", "dense", "forensic"],
    ) -> None:
        self.declared = frozenset(account.account for account in accounts)
        self.ledger = Ledger(self.declared)
        self.tax = TaxBook(profiles, income_sources)
        self.capture = capture
        self.journal: list[JournalEntry] = []
        self.journal_entry_count = 0
        self.transfers: list[TransferOutcome] = []
        self.tax_accruals: list[TaxAccrual] = []
        self.tax_liabilities: list[TaxLiabilityState] = []
        self.tax_payments: list[TaxPaymentOutcome] = []
        self.tax_settlements: list[TaxSettlementOutcome] = []
        self.mortgage_payments: list[MortgagePaymentOutcome] = []
        for account in accounts:
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
        self.ledger.ensure_account(AccountRef(agent_id="__external__", account_id="boundary"))
        for profile in profiles:
            self.ledger.ensure_account(AccountRef(agent_id=profile.agent_id, account_id="asset:tax-prepayments"))
            self.ledger.ensure_account(
                AccountRef(agent_id=profile.tax_authority_agent_id, account_id="income:tax-payments")
            )
            for rules in profile.jurisdictions:
                for kind in ("expense", "liability"):
                    self.ledger.ensure_account(
                        AccountRef(agent_id=profile.agent_id, account_id=f"{kind}:tax:{rules.jurisdiction_id}")
                    )

    def apply(self, entry: JournalEntry) -> None:
        count = self.journal_entry_count + 1
        if count >= 1 << 64:
            raise OverflowError("integer overflow during journal entry count")
        self.ledger.apply(entry)
        self.journal_entry_count = count
        if self.capture == "forensic":
            self.journal.append(entry)

    def apply_entries(self, entries: Sequence[JournalEntry]) -> None:
        count = self.journal_entry_count + len(entries)
        if count >= 1 << 64:
            raise OverflowError("integer overflow during journal entry count")
        candidate = deepcopy(self.ledger)
        for entry in entries:
            candidate.apply(entry)
        self.ledger = candidate
        self.journal_entry_count = count
        if self.capture == "forensic":
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
        candidate = deepcopy(self.tax.income)
        if income is not None:
            candidate.accrue(request.to_account.agent_id, income, request.amount)
        if deduction == "ordinary":
            candidate.deduct_from_ordinary(request.from_account.agent_id, request.amount)
        self.move(month, request.cause_id, request.from_account, request.to_account, request.amount)
        self.tax.income = candidate
        if self.capture != "summary":
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

    def close_tax_year(self, scenario: PreparedScenario, month: int, mortgages: Sequence[Mortgage]) -> None:
        assessments = self.tax.assessments(scenario, month, mortgages)
        # Stage all postings first: a bad jurisdiction must not commit earlier jurisdictions.
        candidate = deepcopy(self.ledger)
        entries = []
        for row in assessments:
            if row.total_tax:
                entry = JournalEntry(
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
                candidate.apply(entry)
                entries.append(entry)
        count = self.journal_entry_count + len(entries)
        if count >= 1 << 64:
            raise OverflowError("integer overflow during journal entry count")
        self.ledger = candidate
        self.journal_entry_count = count
        if self.capture == "forensic":
            self.journal.extend(entries)
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
