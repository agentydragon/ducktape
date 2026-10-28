"""Pure, policy-driven consumption decisions shared by reports and funding estimates."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from zoneinfo import ZoneInfo

from finance.plaid.spend.allowance import (
    AccountingPolicy,
    AllowancePolicy,
    CreditTreatment,
    Disposition,
    Kind,
    Rule,
    Transaction,
    accounting_disposition,
    prepare_accounting,
)


class ConsumptionMode(StrEnum):
    NET = "net"
    GROSS_PURCHASES = "gross_purchases"


@dataclass(frozen=True)
class ConsumptionEntry:
    transaction: Transaction
    report_day: date
    rule: Rule | None
    disposition: Disposition
    amount_minor_units: int

    @property
    def kind(self) -> Kind:
        return self.rule.kind if self.rule is not None else Kind.REVIEW


@dataclass(frozen=True)
class PreparedConsumption:
    entries: list[ConsumptionEntry]
    issues: list[str]


def prepare_consumption(
    allowance: AllowancePolicy,
    transactions: list[Transaction],
    *,
    time_zone: ZoneInfo,
    today: date,
    currency: str,
    include_excluded_categories: Collection[str],
    confirmed_refund_rule_ids: Collection[str],
    mode: ConsumptionMode,
) -> PreparedConsumption:
    """Resolve full-history reversals before callers select their reporting window.

    Retain excluded, foreign-currency and held-credit entries for coverage and audit
    reporting. Out-of-scope accounts, future purchase days and superseded pending
    rows are omitted entirely. Today and pending purchases remain eligible; callers
    choose whether their report needs only completed days or posted transactions.
    Unclassified/review purchases count conservatively. Negative amounts only net
    against spending with an explicitly confirmed refund rule; gross mode counts
    no credits. A validated reversal removes both legs in either mode.
    """
    prepared = prepare_accounting(transactions, allowance=allowance, time_zone=time_zone, today=today)
    accounting_policy = AccountingPolicy(
        currency=currency,
        counted_kinds=frozenset({Kind.FIXED, Kind.FLEXIBLE, Kind.REVIEW}),
        credits=(CreditTreatment.CONFIRMED_REFUNDS if mode == ConsumptionMode.NET else CreditTreatment.GROSS_PURCHASES),
        include_excluded_categories=frozenset(include_excluded_categories),
        confirmed_refund_rule_ids=frozenset(confirmed_refund_rule_ids),
    )
    entries = []
    for entry in prepared.entries:
        if entry.report_day > today or entry.superseded_pending:
            continue
        disposition = accounting_disposition(entry, accounting_policy)
        entries.append(
            ConsumptionEntry(
                transaction=entry.transaction,
                report_day=entry.report_day,
                rule=entry.rule,
                disposition=disposition,
                amount_minor_units=entry.amount_minor_units if disposition == Disposition.COUNTED else 0,
            )
        )
    return PreparedConsumption(entries=entries, issues=prepared.issues)
