"""Project one card's statement from normalized account and transaction facts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from babel.numbers import get_currency_precision

from finance.plaid.spend.allowance import (
    AccountingPolicy,
    CreditTreatment,
    Disposition,
    Kind,
    Transaction,
    accounting_disposition,
    prepare_accounting,
)
from finance.plaid.spend.configuration import CardConfig
from finance.plaid.spend.models import (
    AlertState,
    CardView,
    ProvisionalCardPeriod,
    StatementCycle,
    StatementReason,
    UnavailableCardPeriod,
)


@dataclass(frozen=True)
class CardAccount:
    account_id: str
    account_name: str
    institution_name: str | None
    mask: str | None
    currency: str
    last_synced_at: datetime | None


def project_card(
    account: CardAccount,
    config: CardConfig,
    *,
    cycle_start: date | None,
    statement_available: bool,
    transactions: Sequence[Transaction],
    time_zone: ZoneInfo,
    today: date,
) -> tuple[CardView, dict[tuple[str, str], tuple[StatementReason, int]]]:
    """Compute statement totals and each transaction's posting-date disposition.

    Transactions belong to this account and include retained posted replacements,
    even when their posting date precedes the current cycle.
    """
    if config.account_id != account.account_id:
        raise ValueError("card configuration and account IDs differ")
    common = {
        "account_id": account.account_id,
        "label": config.label,
        "account_name": account.account_name,
        "institution_name": account.institution_name,
        "mask": account.mask,
        "currency": account.currency,
        "limit_minor_units": config.limit_minor_units,
        "alert_threshold_percent": config.alert_threshold_percent,
        "last_synced_at": account.last_synced_at,
    }
    if cycle_start is None:
        return (
            CardView(
                **common,
                statement_period=UnavailableCardPeriod(),
                spend_minor_units=None,
                posted_minor_units=None,
                pending_minor_units=None,
                spend_percent=None,
                alert_state=AlertState.UNAVAILABLE,
            ),
            {},
        )

    posted_minor_units = 0
    pending_minor_units = 0
    decisions: dict[tuple[str, str], tuple[StatementReason, int]] = {}
    prepared = prepare_accounting(
        list(transactions),
        time_zone=time_zone,
        today=today,
        minor_unit_precision=get_currency_precision(account.currency),
    )
    policy = AccountingPolicy(
        currency=account.currency,
        counted_kinds=frozenset(Kind),
        credits=CreditTreatment.ALL,
        honor_reversals=False,
        exclude_card_payments=True,
    )
    for entry in prepared.entries:
        transaction = entry.transaction
        key = (account.account_id, transaction.transaction_id)
        # Statements intentionally use posting dates, not purchase days.
        if not cycle_start <= transaction.date <= today:
            decisions[key] = (StatementReason.OUTSIDE_CYCLE, 0)
            continue
        disposition = accounting_disposition(entry, policy)
        amount_minor_units = entry.amount_minor_units if disposition == Disposition.COUNTED else 0
        decisions[key] = (StatementReason(disposition), amount_minor_units)
        if transaction.pending:
            pending_minor_units += amount_minor_units
        else:
            posted_minor_units += amount_minor_units

    spend_minor_units = posted_minor_units + pending_minor_units
    limit_minor_units = config.limit_minor_units
    spend_percent = (
        Decimal(spend_minor_units) * Decimal(100) / Decimal(limit_minor_units)
        if limit_minor_units is not None and statement_available
        else None
    )
    if not statement_available:
        alert_state = AlertState.UNAVAILABLE
    elif spend_percent is not None and spend_percent >= 100:
        alert_state = AlertState.EXCEEDED
    elif (
        spend_percent is not None
        and config.alert_threshold_percent is not None
        and spend_percent >= config.alert_threshold_percent
    ):
        alert_state = AlertState.WARNING
    else:
        alert_state = AlertState.NORMAL
    return (
        CardView(
            **common,
            statement_period=StatementCycle(start=cycle_start, through=today)
            if statement_available
            else ProvisionalCardPeriod(start=cycle_start, through=today),
            spend_minor_units=spend_minor_units,
            posted_minor_units=posted_minor_units,
            pending_minor_units=pending_minor_units,
            spend_percent=spend_percent,
            alert_state=alert_state,
        ),
        decisions,
    )
