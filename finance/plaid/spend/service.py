"""Read the shared card configuration and compute statement-cycle spend from Plaid's mirror."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import asyncpg
from babel.numbers import get_currency_precision

from finance.plaid.spend.allowance import AllowanceView, PaceAlert, Status, Transaction, TransactionDecision, calculate
from finance.plaid.spend.models import (
    AlertState,
    AllowanceConfigurationView,
    CardConfigurationView,
    CardView,
    PlaidTransactionDetails,
    SpendConfiguration,
    SpendConfigurationView,
    SpendTransactionRow,
    SpendTransactionsView,
    SpendView,
    StatementReason,
)

logger = logging.getLogger(__name__)

_CHANNEL = "plaid_spend_changed"
_CARD_PAYMENT_CATEGORY = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
_COUNTERPARTIES_COLUMN = (
    "CASE WHEN jsonb_typeof(t.raw_json->'counterparties') = 'array' "
    "THEN (t.raw_json->'counterparties')::text ELSE '[]' END AS counterparties"
)


class SpendService:
    """Postgres reader and reconnecting NOTIFY subscriber for one shared card view."""

    def __init__(self, database_url: str, configuration: SpendConfiguration, *, dashboard_url: str) -> None:
        self._database_url = database_url
        self._configuration = configuration
        self._dashboard_url = dashboard_url
        self._pool: asyncpg.Pool | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._listening = asyncio.Event()
        self._revision = 0
        self._subscribers: set[asyncio.Queue[None]] = set()

    @property
    def listening(self) -> asyncio.Event:
        return self._listening

    @property
    def revision(self) -> int:
        return self._revision

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=8)
        self._listener_task = asyncio.create_task(self._listen_forever(), name="plaid-spend-listener")

    async def close(self) -> None:
        self._stopping.set()
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def read_configuration(self) -> SpendConfigurationView:
        configuration = self._configuration
        policy = configuration.allowance
        allowance = None
        if policy is not None:
            allowance = AllowanceConfigurationView(
                monthly_minor_units=policy.monthly_minor_units,
                activation_at=policy.activation_at,
                currency=policy.currency,
                spending_account_count=len(policy.spending_account_ids),
                max_sync_age_hours=policy.max_sync_age_hours,
                rules=policy.rules,
            )
        return SpendConfigurationView(
            cards=[
                CardConfigurationView(
                    label=card.label,
                    enabled=card.enabled,
                    limit_minor_units=card.limit_minor_units,
                    alert_threshold_percent=card.alert_threshold_percent,
                )
                for card in configuration.cards
            ],
            allowance=allowance,
        )

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)

    async def read_view(
        self,
        *,
        allowance_decisions: list[TransactionDecision] | None = None,
        account_labels: dict[str, str] | None = None,
        card_transactions: list[asyncpg.Record] | None = None,
        statement_decisions: dict[tuple[str, str], tuple[StatementReason, int]] | None = None,
        transaction_details: dict[tuple[str, str], PlaidTransactionDetails] | None = None,
    ) -> SpendView:
        generated_at = datetime.now(UTC)
        today = generated_at.date()
        card_configs = {card.account_id: card for card in self._configuration.cards if card.enabled}
        if not card_configs:
            return SpendView(
                generated_at=generated_at,
                cards=[],
                allowance=await self._read_allowance(
                    generated_at, allowance_decisions, account_labels, transaction_details
                ),
                dashboard_url=self._dashboard_url,
            )

        pool = self._require_pool()
        async with pool.acquire() as connection:
            account_rows = await connection.fetch(
                """
                SELECT a.account_id, a.name AS account_name, a.mask,
                       COALESCE(
                           a.iso_currency_code,
                           a.raw_json #>> '{balances,iso_currency_code}',
                           a.raw_json #>> '{balances,unofficial_currency_code}',
                           'USD'
                       ) AS currency,
                       l.institution_name,
                       l.item_id, l.last_synced_at
                FROM public.accounts AS a
                JOIN public.links AS l ON l.item_id = a.item_id
                WHERE a.account_id = ANY($1::text[])
                  AND a.type = 'credit' AND l.status = 'active'
                ORDER BY a.account_id
                """,
                list(card_configs),
            )
            if not account_rows:
                return SpendView(
                    generated_at=generated_at,
                    cards=[],
                    allowance=await self._read_allowance(
                        generated_at, allowance_decisions, account_labels, transaction_details
                    ),
                    dashboard_url=self._dashboard_url,
                )

            account_ids = [row["account_id"] for row in account_rows]
            liability_rows = await connection.fetch(
                """
                SELECT DISTINCT ON (lc.account_id)
                       lc.account_id, lc.raw_json->>'last_statement_issue_date' AS last_statement_issue_date
                FROM public.liability_credit_snapshots AS lc
                JOIN public.accounts AS a
                  ON a.account_id = lc.account_id AND a.item_id = lc.item_id
                JOIN public.links AS l ON l.item_id = a.item_id
                WHERE lc.account_id = ANY($1::text[]) AND l.status = 'active'
                ORDER BY lc.account_id, lc.captured_at DESC, lc.id DESC
                """,
                account_ids,
            )
            cycle_starts: dict[str, date] = {}
            for row in liability_rows:
                if (cycle_start := _cycle_start(row["last_statement_issue_date"])) is not None:
                    cycle_starts[row["account_id"]] = cycle_start
            statement_account_ids = set(cycle_starts)
            missing_statement_ids = [row["account_id"] for row in account_rows if row["account_id"] not in cycle_starts]
            if missing_statement_ids:
                first_transactions = await connection.fetch(
                    """SELECT t.account_id, MIN(t.date) AS first_date
                       FROM public.transactions t
                       JOIN public.accounts a ON a.account_id = t.account_id AND a.item_id = t.item_id
                       JOIN public.links l ON l.item_id = a.item_id
                       WHERE t.account_id = ANY($1::text[]) AND t.date <= $2
                         AND t.removed IS FALSE AND l.status = 'active'
                       GROUP BY t.account_id""",
                    missing_statement_ids,
                    today,
                )
                cycle_starts.update({row["account_id"]: row["first_date"] for row in first_transactions})
            counted_accounts = [row for row in account_rows if row["account_id"] in cycle_starts]
            transaction_rows: list[asyncpg.Record] = []
            if counted_accounts:
                first_cycle_start = min(cycle_starts[row["account_id"]] for row in counted_accounts)
                raw_column = "t.raw_json," if transaction_details is not None else ""
                counterparties_column = f"{_COUNTERPARTIES_COLUMN}," if transaction_details is not None else ""
                transaction_rows = await connection.fetch(
                    f"""
                    SELECT t.account_id, t.transaction_id, t.date, t.amount, t.pending,
                           t.pending_transaction_id,
                           t.name, t.merchant_name, t.pfc_primary,
                           {raw_column}
                           {counterparties_column}
                           t.raw_json->>'merchant_category_code' AS merchant_category_code,
                           COALESCE(t.iso_currency_code, t.raw_json->>'unofficial_currency_code') AS currency,
                           COALESCE(
                               t.pfc_detailed,
                               t.raw_json #>> '{{personal_finance_category,detailed}}'
                           ) AS pfc_detailed
                    FROM public.transactions AS t
                    JOIN public.accounts AS a
                      ON a.account_id = t.account_id AND a.item_id = t.item_id
                    JOIN public.links AS l ON l.item_id = a.item_id
                    WHERE t.account_id = ANY($1::text[])
                      AND t.date <= $3
                      AND (
                          t.date >= $2
                          OR (t.pending IS FALSE AND t.pending_transaction_id IS NOT NULL)
                      )
                      AND t.removed IS FALSE AND l.status = 'active'
                    """,
                    [row["account_id"] for row in counted_accounts],
                    min(first_cycle_start, today - timedelta(days=29))
                    if card_transactions is not None
                    else first_cycle_start,
                    today,
                )

        if card_transactions is not None:
            card_transactions.extend(transaction_rows)
        if account_labels is not None:
            account_labels.update({row["account_id"]: card_configs[row["account_id"]].label for row in account_rows})

        superseded_pending_ids = {
            (row["account_id"], row["pending_transaction_id"])
            for row in transaction_rows
            if not row["pending"] and row["pending_transaction_id"] is not None
        }
        transactions_by_account: dict[str, list[asyncpg.Record]] = {}
        for row in transaction_rows:
            transactions_by_account.setdefault(row["account_id"], []).append(row)

        cards = []
        for account in account_rows:
            account_id = account["account_id"]
            card_config = card_configs[account_id]
            if (cycle_start := cycle_starts.get(account_id)) is None:
                cards.append(
                    CardView(
                        account_id=account_id,
                        label=card_config.label,
                        account_name=account["account_name"],
                        institution_name=account["institution_name"],
                        mask=account["mask"],
                        currency=account["currency"],
                        cycle_start=None,
                        spend_minor_units=None,
                        posted_minor_units=None,
                        pending_minor_units=None,
                        limit_minor_units=card_config.limit_minor_units,
                        alert_threshold_percent=card_config.alert_threshold_percent,
                        spend_percent=None,
                        alert_state=AlertState.UNAVAILABLE,
                        last_synced_at=_as_utc(account["last_synced_at"]),
                        statement_available=False,
                    )
                )
                continue

            posted_minor_units = 0
            pending_minor_units = 0
            for transaction in transactions_by_account.get(account_id, []):
                key = (account_id, transaction["transaction_id"])
                if transaction["date"] < cycle_start or transaction["date"] > today:
                    if statement_decisions is not None:
                        statement_decisions[key] = (StatementReason.OUTSIDE_CYCLE, 0)
                    continue
                if transaction["pending"] and (account_id, transaction["transaction_id"]) in superseded_pending_ids:
                    if statement_decisions is not None:
                        statement_decisions[key] = (StatementReason.SUPERSEDED_PENDING, 0)
                    continue
                if transaction["pfc_detailed"] == _CARD_PAYMENT_CATEGORY:
                    if statement_decisions is not None:
                        statement_decisions[key] = (StatementReason.CARD_PAYMENT, 0)
                    continue
                if transaction["currency"] is not None and transaction["currency"] != account["currency"]:
                    if statement_decisions is not None:
                        statement_decisions[key] = (StatementReason.OTHER_CURRENCY, 0)
                    continue
                amount_minor_units = _amount_minor_units(Decimal(str(transaction["amount"])), account["currency"])
                if statement_decisions is not None:
                    statement_decisions[key] = (StatementReason.COUNTED, amount_minor_units)
                if transaction["pending"]:
                    pending_minor_units += amount_minor_units
                else:
                    posted_minor_units += amount_minor_units

            spend_minor_units = posted_minor_units + pending_minor_units
            limit_minor_units = card_config.limit_minor_units
            statement_available = account_id in statement_account_ids
            spend_percent = (
                float(Decimal(spend_minor_units) * Decimal(100) / Decimal(limit_minor_units))
                if limit_minor_units is not None and statement_available
                else None
            )
            if not statement_available:
                alert_state = AlertState.UNAVAILABLE
            elif spend_percent is not None and spend_percent >= 100:
                alert_state = AlertState.EXCEEDED
            elif (
                spend_percent is not None
                and card_config.alert_threshold_percent is not None
                and spend_percent >= card_config.alert_threshold_percent
            ):
                alert_state = AlertState.WARNING
            else:
                alert_state = AlertState.NORMAL
            cards.append(
                CardView(
                    account_id=account_id,
                    label=card_config.label,
                    account_name=account["account_name"],
                    institution_name=account["institution_name"],
                    mask=account["mask"],
                    currency=account["currency"],
                    cycle_start=cycle_start,
                    spend_minor_units=spend_minor_units,
                    posted_minor_units=posted_minor_units,
                    pending_minor_units=pending_minor_units,
                    limit_minor_units=limit_minor_units,
                    alert_threshold_percent=card_config.alert_threshold_percent,
                    spend_percent=spend_percent,
                    alert_state=alert_state,
                    last_synced_at=_as_utc(account["last_synced_at"]),
                    statement_available=statement_available,
                )
            )
        return SpendView(
            generated_at=generated_at,
            cards=cards,
            allowance=await self._read_allowance(
                generated_at, allowance_decisions, account_labels, transaction_details
            ),
            dashboard_url=self._dashboard_url,
        )

    async def read_transactions(self, window: Literal["7d", "30d", "cycle"] = "30d") -> SpendTransactionsView:
        decisions: list[TransactionDecision] = []
        labels: dict[str, str] = {}
        card_rows: list[asyncpg.Record] = []
        statement_decisions: dict[tuple[str, str], tuple[StatementReason, int]] = {}
        details_by_key: dict[tuple[str, str], PlaidTransactionDetails] = {}
        view = await self.read_view(
            allowance_decisions=decisions,
            account_labels=labels,
            card_transactions=card_rows,
            statement_decisions=statement_decisions,
            transaction_details=details_by_key,
        )
        today = view.generated_at.date()
        if window == "7d":
            start = today - timedelta(days=6)
        elif window == "cycle" and view.allowance is not None and view.allowance.status == Status.ACTIVE:
            start = view.allowance.current_cycle_start or today
        else:
            start = today - timedelta(days=29)
        card_currencies = {card.account_id: card.currency for card in view.cards}
        card_cycle_starts = {card.account_id: card.cycle_start for card in view.cards}
        card_ids = {card.account_id for card in self._configuration.cards if card.enabled}
        allowance_ids = self._configuration.allowance.spending_account_ids if self._configuration.allowance else set()
        for row in card_rows:
            details_by_key.setdefault((row["account_id"], row["transaction_id"]), _plaid_details(row["raw_json"]))
        labels.update({card.account_id: card.label for card in self._configuration.cards if card.enabled})
        rows: list[SpendTransactionRow] = []
        seen: set[tuple[str, str]] = set()

        def add_row(transaction: Transaction, decision: TransactionDecision | None) -> None:
            if not start <= transaction.date <= today:
                return
            key = (transaction.account_id, transaction.transaction_id)
            if key in seen:
                return
            seen.add(key)
            statement = statement_decisions.get(key)
            details = details_by_key.get(key, PlaidTransactionDetails())
            is_card = transaction.account_id in card_ids
            currency = transaction.currency or card_currencies.get(transaction.account_id) or "USD"
            card_cycle_start = card_cycle_starts.get(transaction.account_id)
            if statement is not None:
                statement_reason = statement[0]
            elif is_card and card_cycle_start is not None and transaction.date < card_cycle_start:
                statement_reason = StatementReason.OUTSIDE_CYCLE
            elif is_card:
                statement_reason = StatementReason.UNAVAILABLE
            else:
                statement_reason = None
            rows.append(
                SpendTransactionRow(
                    date=transaction.date,
                    account_label=labels.get(transaction.account_id, "Account"),
                    name=transaction.name,
                    merchant_name=transaction.merchant_name,
                    amount_minor_units=_amount_minor_units(transaction.amount, currency),
                    currency=currency,
                    pending=transaction.pending,
                    allowance_in_scope=transaction.account_id in allowance_ids,
                    disposition=decision.disposition if decision else None,
                    rule_number=decision.rule_number if decision else None,
                    rule=decision.rule if decision else None,
                    allowance_minor_units=decision.allowance_minor_units if decision else 0,
                    trailing_7_pace_minor_units=decision.trailing_7_pace_minor_units if decision else 0,
                    trailing_30_pace_minor_units=decision.trailing_30_pace_minor_units if decision else 0,
                    statement_minor_units=statement[1]
                    if statement
                    else 0
                    if statement_reason == StatementReason.OUTSIDE_CYCLE
                    else None,
                    statement_reason=statement_reason,
                    pfc_primary=transaction.pfc_primary,
                    pfc_detailed=transaction.pfc_detailed,
                    merchant_category_code=transaction.merchant_category_code,
                    counterparties=transaction.counterparties or [],
                    details=details,
                )
            )

        for decision in decisions:
            add_row(decision.transaction, decision)
        for row in card_rows:
            if (row["account_id"], row["transaction_id"]) in seen:
                continue
            add_row(
                Transaction(
                    account_id=row["account_id"],
                    transaction_id=row["transaction_id"],
                    pending_transaction_id=row["pending_transaction_id"],
                    date=row["date"],
                    amount=Decimal(str(row["amount"])),
                    pending=row["pending"],
                    name=row["name"],
                    merchant_name=row["merchant_name"],
                    pfc_primary=row["pfc_primary"],
                    pfc_detailed=row["pfc_detailed"],
                    currency=row["currency"],
                    merchant_category_code=row["merchant_category_code"],
                    counterparties=row["counterparties"],
                ),
                None,
            )
        rows.sort(key=lambda row: (row.date, abs(row.amount_minor_units)), reverse=True)
        return SpendTransactionsView(
            generated_at=view.generated_at, window=window, window_start=start, allowance=view.allowance, rows=rows
        )

    async def _read_allowance(
        self,
        now: datetime,
        decisions: list[TransactionDecision] | None = None,
        account_labels: dict[str, str] | None = None,
        transaction_details: dict[tuple[str, str], PlaidTransactionDetails] | None = None,
    ) -> AllowanceView | None:
        policy = self._configuration.allowance
        if policy is None:
            return None
        # No account IDs or names leave the server in the allowance view.
        async with self._require_pool().acquire() as connection:
            accounts = await connection.fetch(
                """SELECT a.account_id, a.name, a.type, l.last_synced_at
                   FROM public.accounts a JOIN public.links l ON l.item_id = a.item_id
                   WHERE a.account_id = ANY($1::text[]) AND l.status = 'active'""",
                list(policy.spending_account_ids),
            )
            synced = [_as_utc(row["last_synced_at"]) for row in accounts]
            if account_labels is not None:
                for row in accounts:
                    account_labels.setdefault(
                        row["account_id"], self._configuration.account_labels.get(row["account_id"], row["name"])
                    )
            last_synced = min((value for value in synced if value is not None), default=None)
            if (
                len(accounts) != len(policy.spending_account_ids)
                or any(row["type"] not in ("credit", "depository") for row in accounts)
                or any(value is None or now - value > timedelta(hours=policy.max_sync_age_hours) for value in synced)
            ):
                return AllowanceView(
                    status=Status.UNAVAILABLE,
                    currency=policy.currency,
                    monthly_minor_units=policy.monthly_minor_units,
                    activation_at=policy.activation_at,
                    available_minor_units=None,
                    next_credit_at=None,
                    posted_minor_units=0,
                    pending_minor_units=0,
                    review_minor_units=0,
                    review_transaction_count=0,
                    unmatched_refunds_minor_units=0,
                    windows_minor_units=None,
                    trailing_7_daily_minor_units=None,
                    trailing_7_observed_daily_minor_units=None,
                    trailing_30_observed_daily_minor_units=None,
                    trailing_7_unmatched_count=None,
                    trailing_7_unmatched_minor_units=None,
                    estimated_exhaustion_at=None,
                    alert_state=PaceAlert.UNAVAILABLE,
                    spending_signal=PaceAlert.UNAVAILABLE,
                    last_synced_at=last_synced,
                    note="Account coverage or sync freshness unavailable; do not rely on the allowance.",
                )
            rows = []
            if policy.activation_at <= now.date():
                raw_column = "t.raw_json," if transaction_details is not None else ""
                rows = await connection.fetch(
                    f"""SELECT t.account_id, t.transaction_id, t.pending_transaction_id,
                              t.date, t.amount, t.pending, t.name, t.merchant_name,
                              t.pfc_primary, t.pfc_detailed,
                              {raw_column}
                              a.type AS account_type,
                              t.raw_json->>'merchant_category_code' AS merchant_category_code,
                              {_COUNTERPARTIES_COLUMN},
                              COALESCE(t.iso_currency_code, t.raw_json->>'unofficial_currency_code') AS currency
                       FROM public.transactions t
                       JOIN public.accounts a ON a.account_id = t.account_id AND a.item_id = t.item_id
                       JOIN public.links l ON l.item_id = a.item_id
                       WHERE t.account_id = ANY($1::text[]) AND t.date >= $2 AND t.date <= $3
                         AND t.removed IS FALSE AND l.status = 'active'""",
                    list(policy.spending_account_ids),
                    min(policy.activation_at, (now - timedelta(days=29)).date()),
                    now.date(),
                )
            if transaction_details is not None:
                transaction_details.update(
                    {(row["account_id"], row["transaction_id"]): _plaid_details(row["raw_json"]) for row in rows}
                )
        return calculate(
            policy,
            [Transaction.model_validate(dict(row)) for row in rows],
            now=now,
            last_synced_at=last_synced,
            decisions=decisions,
        )

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("spend database pool is not initialized")
        return self._pool

    async def _listen_forever(self) -> None:
        while not self._stopping.is_set():
            connection: asyncpg.Connection | None = None
            try:
                connection = await asyncpg.connect(self._database_url)
                terminated = asyncio.Event()
                connection.add_termination_listener(lambda _, event=terminated: event.set())
                await connection.add_listener(_CHANNEL, self._on_notification)
                self._listening.set()
                self._wake_subscribers()
                stopped_task = asyncio.create_task(self._stopping.wait())
                terminated_task = asyncio.create_task(terminated.wait())
                try:
                    await asyncio.wait({stopped_task, terminated_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stopped_task.cancel()
                    terminated_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stopped_task
                    with contextlib.suppress(asyncio.CancelledError):
                        await terminated_task
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as error:
                logger.warning("Postgres spend listener disconnected (%s); reconnecting", type(error).__name__)
            finally:
                if self._listening.is_set():
                    self._listening.clear()
                    self._wake_subscribers()
                if connection is not None:
                    await connection.close()
            if not self._stopping.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=3)

    def _on_notification(self, connection: object, pid: int, channel: str, payload: object) -> None:
        del connection, pid, channel, payload
        self._wake_subscribers()

    def _wake_subscribers(self) -> None:
        self._revision += 1
        for queue in self._subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)


def _cycle_start(last_statement_issue_date: str | None) -> date | None:
    if last_statement_issue_date is None:
        return None
    return date.fromisoformat(last_statement_issue_date) + timedelta(days=1)


def _amount_minor_units(major_units: Decimal, currency: str) -> int:
    precision = get_currency_precision(currency)
    scale = 10**precision
    quantum = Decimal(1).scaleb(-precision)
    rounded = major_units.quantize(quantum, rounding=ROUND_HALF_UP)
    return int(rounded * scale)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _plaid_details(value: str | dict[str, object]) -> PlaidTransactionDetails:
    """Parse documented Plaid fields from asyncpg's jsonb value."""
    payload = json.loads(value, parse_float=Decimal) if isinstance(value, str) else value
    return PlaidTransactionDetails.model_validate(payload)
