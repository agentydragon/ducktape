"""Period-oriented API projections of the existing Spend calculation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from finance.plaid.spend.allowance import AllowanceView, Disposition, PaceAlert, PlaidCounterparty, Rule, Status
from finance.plaid.spend.models import (
    AlertState,
    CardView,
    PlaidTransactionDetails,
    SpendTransactionRow,
    SpendTransactionsView,
    SpendView,
    StatementReason,
)

type PeriodId = Literal["credit_cycle", "calendar_month", "year_to_date", "rolling_7d", "rolling_30d"]
type TransactionPeriodId = Literal["credit_cycle", "rolling_7d", "rolling_30d"]
type PacePeriodId = Literal["rolling_7d", "rolling_30d"]

TRANSACTION_WINDOWS: dict[TransactionPeriodId, Literal["cycle", "7d", "30d"]] = {
    "credit_cycle": "cycle",
    "rolling_7d": "7d",
    "rolling_30d": "30d",
}


class Period(BaseModel):
    """Inclusive dates represented by one report, through the view's observation date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: PeriodId
    start: date
    end: date


class AllowanceSpendPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period: Period
    counted_from: date = Field(description="Allowance spend starts no earlier than activation.")
    spend_minor_units: int


class UnmatchedCharges(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    amount_minor_units: int


class RecordedPacePeriod(BaseModel):
    """Positive recorded purchases can include history before allowance activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period: Period
    observed_daily_minor_units: int | None
    unmatched_charges: UnmatchedCharges | None = None


class ForecastView(BaseModel):
    """Burst-adjusted estimate, distinct from a recorded rolling average."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    basis_period_id: Literal["rolling_7d"] = "rolling_7d"
    daily_pace_minor_units: int | None
    projected_cycle_end_minor_units: int | None
    estimated_exhaustion_at: datetime | None
    alert_state: PaceAlert


class AllowanceReportView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Status
    currency: str
    monthly_minor_units: int
    activation_at: date
    available_minor_units: int | None
    next_credit_at: datetime | None
    posted_minor_units: int
    pending_minor_units: int
    review_minor_units: int
    review_transaction_count: int
    unmatched_refunds_minor_units: int
    spend_periods: list[AllowanceSpendPeriod]
    recorded_pace_periods: list[RecordedPacePeriod]
    forecast: ForecastView
    spending_signal: PaceAlert
    last_synced_at: datetime | None
    note: str | None
    prior_carry_minor_units: int


class StatementCycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["statement"] = "statement"
    start: date
    through: date


class ProvisionalCardPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["provisional"] = "provisional"
    start: date
    through: date


class UnavailableCardPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["unavailable"] = "unavailable"


type CardPeriod = StatementCycle | ProvisionalCardPeriod | UnavailableCardPeriod


class CardReportView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: str
    label: str
    account_name: str
    institution_name: str | None
    mask: str | None
    currency: str
    statement_period: Annotated[CardPeriod, Field(discriminator="kind")]
    spend_minor_units: int | None
    posted_minor_units: int | None
    pending_minor_units: int | None
    limit_minor_units: int | None
    alert_threshold_percent: int | None
    spend_percent: float | None
    alert_state: AlertState
    last_synced_at: datetime | None


class SpendReportView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    cards: list[CardReportView]
    allowance: AllowanceReportView | None
    dashboard_url: str | None


class PaceEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period_id: PacePeriodId
    amount_minor_units: int


class SpendTransactionReportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    account_label: str
    name: str
    merchant_name: str | None
    amount_minor_units: int
    currency: str
    pending: bool
    allowance_in_scope: bool
    disposition: Disposition | None
    rule_number: int | None
    rule: Rule | None
    allowance_minor_units: int
    pace_effects: list[PaceEffect]
    statement_minor_units: int | None
    statement_reason: StatementReason | None
    pfc_primary: str | None
    pfc_detailed: str | None
    merchant_category_code: str | None
    analysis_category_label: str | None
    counterparties: list[PlaidCounterparty]
    details: PlaidTransactionDetails


class TransactionPeriodSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_count: int
    net_allowance_spend_minor_units: int
    unmatched_charge_count: int
    unmatched_charge_minor_units: int


class SpendTransactionsReportView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    requested_period_id: TransactionPeriodId
    period: Period
    summary: TransactionPeriodSummary
    allowance: AllowanceReportView | None
    rows: list[SpendTransactionReportRow]


def _period(id: PeriodId, today: date, cycle_start: date) -> Period:
    start = {
        "credit_cycle": cycle_start,
        "calendar_month": today.replace(day=1),
        "year_to_date": date(today.year, 1, 1),
        "rolling_7d": today - timedelta(days=6),
        "rolling_30d": today - timedelta(days=29),
    }[id]
    return Period(id=id, start=start, end=today)


def project_allowance(allowance: AllowanceView | None, generated_at: datetime) -> AllowanceReportView | None:
    if allowance is None:
        return None
    spend_periods: list[AllowanceSpendPeriod] = []
    recorded_pace_periods: list[RecordedPacePeriod] = []
    if allowance.status == Status.ACTIVE:
        windows = allowance.windows_minor_units
        cycle_start = allowance.current_cycle_start
        if windows is None or cycle_start is None:
            raise ValueError("active allowance requires reporting windows and a credit-cycle start")
        today = generated_at.date()
        spend_by_period: list[tuple[PeriodId, int]] = [
            ("credit_cycle", windows.current_credit_cycle_minor_units),
            ("calendar_month", windows.calendar_month_minor_units),
            ("year_to_date", windows.year_to_date_minor_units),
            ("rolling_7d", windows.trailing_7_days_minor_units),
            ("rolling_30d", windows.trailing_30_days_minor_units),
        ]
        for period_id, amount in spend_by_period:
            period = _period(period_id, today, cycle_start)
            spend_periods.append(
                AllowanceSpendPeriod(
                    period=period, counted_from=max(period.start, allowance.activation_at), spend_minor_units=amount
                )
            )
        unmatched = (
            UnmatchedCharges(
                count=allowance.trailing_7_unmatched_count,
                amount_minor_units=allowance.trailing_7_unmatched_minor_units or 0,
            )
            if allowance.trailing_7_unmatched_count is not None
            else None
        )
        recorded_pace_periods = [
            RecordedPacePeriod(
                period=_period("rolling_7d", today, cycle_start),
                observed_daily_minor_units=allowance.trailing_7_observed_daily_minor_units,
                unmatched_charges=unmatched,
            ),
            RecordedPacePeriod(
                period=_period("rolling_30d", today, cycle_start),
                observed_daily_minor_units=allowance.trailing_30_observed_daily_minor_units,
            ),
        ]
    return AllowanceReportView(
        status=allowance.status,
        currency=allowance.currency,
        monthly_minor_units=allowance.monthly_minor_units,
        activation_at=allowance.activation_at,
        available_minor_units=allowance.available_minor_units,
        next_credit_at=allowance.next_credit_at,
        posted_minor_units=allowance.posted_minor_units,
        pending_minor_units=allowance.pending_minor_units,
        review_minor_units=allowance.review_minor_units,
        review_transaction_count=allowance.review_transaction_count,
        unmatched_refunds_minor_units=allowance.unmatched_refunds_minor_units,
        spend_periods=spend_periods,
        recorded_pace_periods=recorded_pace_periods,
        forecast=ForecastView(
            daily_pace_minor_units=allowance.trailing_7_daily_minor_units,
            projected_cycle_end_minor_units=allowance.projected_cycle_end_minor_units,
            estimated_exhaustion_at=allowance.estimated_exhaustion_at,
            alert_state=allowance.alert_state,
        ),
        spending_signal=allowance.spending_signal,
        last_synced_at=allowance.last_synced_at,
        note=allowance.note,
        prior_carry_minor_units=allowance.prior_carry_minor_units,
    )


def _project_card(card: CardView, today: date) -> CardReportView:
    statement_period: CardPeriod
    if card.statement_available and card.cycle_start is not None:
        statement_period = StatementCycle(start=card.cycle_start, through=today)
    elif card.cycle_start is not None:
        statement_period = ProvisionalCardPeriod(start=card.cycle_start, through=today)
    else:
        statement_period = UnavailableCardPeriod()
    return CardReportView(
        account_id=card.account_id,
        label=card.label,
        account_name=card.account_name,
        institution_name=card.institution_name,
        mask=card.mask,
        currency=card.currency,
        statement_period=statement_period,
        spend_minor_units=card.spend_minor_units,
        posted_minor_units=card.posted_minor_units,
        pending_minor_units=card.pending_minor_units,
        limit_minor_units=card.limit_minor_units,
        alert_threshold_percent=card.alert_threshold_percent,
        spend_percent=card.spend_percent,
        alert_state=card.alert_state,
        last_synced_at=card.last_synced_at,
    )


def project_view(view: SpendView) -> SpendReportView:
    return SpendReportView(
        generated_at=view.generated_at,
        cards=[_project_card(card, view.generated_at.date()) for card in view.cards],
        allowance=project_allowance(view.allowance, view.generated_at),
        dashboard_url=view.dashboard_url,
    )


def _project_row(row: SpendTransactionRow) -> SpendTransactionReportRow:
    return SpendTransactionReportRow(
        date=row.date,
        account_label=row.account_label,
        name=row.name,
        merchant_name=row.merchant_name,
        amount_minor_units=row.amount_minor_units,
        currency=row.currency,
        pending=row.pending,
        allowance_in_scope=row.allowance_in_scope,
        disposition=row.disposition,
        rule_number=row.rule_number,
        rule=row.rule,
        allowance_minor_units=row.allowance_minor_units,
        pace_effects=[
            PaceEffect(period_id="rolling_7d", amount_minor_units=row.trailing_7_pace_minor_units),
            PaceEffect(period_id="rolling_30d", amount_minor_units=row.trailing_30_pace_minor_units),
        ],
        statement_minor_units=row.statement_minor_units,
        statement_reason=row.statement_reason,
        pfc_primary=row.pfc_primary,
        pfc_detailed=row.pfc_detailed,
        merchant_category_code=row.merchant_category_code,
        analysis_category_label=row.analysis_category_label,
        counterparties=row.counterparties,
        details=row.details,
    )


def project_transactions(
    view: SpendTransactionsView, requested_period_id: TransactionPeriodId
) -> SpendTransactionsReportView:
    actual_period_id: PeriodId = requested_period_id
    if requested_period_id == "credit_cycle" and (view.allowance is None or view.allowance.status != Status.ACTIVE):
        actual_period_id = "rolling_30d"
    unmatched_rows = [
        row
        for row in view.rows
        if row.allowance_in_scope
        and row.disposition == Disposition.COUNTED
        and row.amount_minor_units > 0
        and (row.rule is None or row.rule.kind == "review")
    ]
    return SpendTransactionsReportView(
        generated_at=view.generated_at,
        requested_period_id=requested_period_id,
        period=Period(id=actual_period_id, start=view.window_start, end=view.generated_at.date()),
        summary=TransactionPeriodSummary(
            transaction_count=len(view.rows),
            net_allowance_spend_minor_units=sum(row.allowance_minor_units for row in view.rows),
            unmatched_charge_count=len(unmatched_rows),
            unmatched_charge_minor_units=sum(row.allowance_minor_units for row in unmatched_rows),
        ),
        allowance=project_allowance(view.allowance, view.generated_at),
        rows=[_project_row(row) for row in view.rows],
    )
