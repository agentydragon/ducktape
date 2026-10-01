"""Read per-user card settings and compute statement-cycle spend from Plaid's mirror."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

import asyncpg
from asyncpg.exceptions import InvalidSchemaNameError
from babel.numbers import get_currency_precision

from finance.plaid.spend.models import AccountOption, AlertState, CardConfig, CardConfiguration, CardView, SpendView

logger = logging.getLogger(__name__)

_CHANNEL = "plaid_spend_changed"
_CONFIG_CHANNEL = "plaid_spend_config_changed"
_CARD_PAYMENT_CATEGORY = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"


class UnknownCreditAccountsError(ValueError):
    """A requested config names an account that is not a current credit account."""

    def __init__(self, account_ids: tuple[str, ...]) -> None:
        super().__init__("configuration contains unknown or inactive Plaid credit accounts")
        self.account_ids = account_ids


class SpendService:
    """Postgres reader, config writer and reconnecting NOTIFY subscriber."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: asyncpg.Pool | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._listening = asyncio.Event()
        self._revision = 0
        self._subscribers: set[asyncio.Queue[None]] = set()
        self._instance_id = uuid4().hex

    @property
    def listening(self) -> asyncio.Event:
        return self._listening

    @property
    def revision(self) -> int:
        return self._revision

    async def start(self) -> None:
        pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=8)
        self._pool = pool
        try:
            await self._initialize_configuration_table()
        except BaseException:
            await pool.close()
            self._pool = None
            raise
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

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)

    async def _initialize_configuration_table(self) -> None:
        pool = self._require_pool()
        while not self._stopping.is_set():
            try:
                async with pool.acquire() as connection:
                    await connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS plaid_spend.card_configs (
                            subject TEXT NOT NULL CHECK (length(btrim(subject)) > 0),
                            account_id TEXT NOT NULL CHECK (length(btrim(account_id)) > 0),
                            label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 80),
                            limit_minor_units BIGINT CHECK (
                                limit_minor_units IS NULL OR limit_minor_units > 0
                            ),
                            alert_threshold_percent INTEGER CHECK (
                                alert_threshold_percent IS NULL
                                OR alert_threshold_percent BETWEEN 1 AND 100
                            ),
                            enabled BOOLEAN NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                            PRIMARY KEY (subject, account_id)
                        )
                        """
                    )
                return
            except InvalidSchemaNameError:
                logger.info("plaid_spend schema is not provisioned yet; retrying table initialization")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=3)

    async def list_credit_accounts(self) -> tuple[AccountOption, ...]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT a.account_id,
                       a.name AS account_name,
                       COALESCE(l.institution_name, l.label, 'Unknown institution') AS institution_name,
                       a.mask,
                       COALESCE(
                           a.iso_currency_code,
                           a.raw_json #>> '{balances,iso_currency_code}',
                           a.raw_json #>> '{balances,unofficial_currency_code}',
                           'USD'
                       ) AS currency
                FROM public.accounts AS a
                JOIN public.links AS l ON l.item_id = a.item_id
                WHERE a.type = 'credit' AND l.status = 'active'
                ORDER BY institution_name, a.name, a.account_id
                """
            )
        return tuple(
            AccountOption(
                account_id=row["account_id"],
                account_name=row["account_name"],
                institution_name=row["institution_name"],
                mask=row["mask"],
                currency=row["currency"],
            )
            for row in rows
        )

    async def get_configuration(self, subject: str) -> CardConfiguration:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT account_id, label, limit_minor_units, alert_threshold_percent, enabled
                FROM plaid_spend.card_configs
                WHERE subject = $1
                ORDER BY account_id
                """,
                subject,
            )
        return CardConfiguration(
            cards=[
                CardConfig(
                    account_id=row["account_id"],
                    label=row["label"],
                    limit_minor_units=row["limit_minor_units"],
                    alert_threshold_percent=row["alert_threshold_percent"],
                    enabled=row["enabled"],
                )
                for row in rows
            ]
        )

    async def replace_configuration(self, subject: str, configuration: CardConfiguration) -> None:
        pool = self._require_pool()
        account_ids = tuple(card.account_id for card in configuration.cards)
        async with pool.acquire() as connection, connection.transaction():
            if account_ids:
                actual_account_ids = {
                    row["account_id"]
                    for row in await connection.fetch(
                        """
                        SELECT a.account_id
                        FROM public.accounts AS a
                        JOIN public.links AS l ON l.item_id = a.item_id
                        WHERE a.type = 'credit' AND l.status = 'active'
                          AND a.account_id = ANY($1::text[])
                        """,
                        list(account_ids),
                    )
                }
                unknown_ids = tuple(sorted(set(account_ids) - actual_account_ids))
                if unknown_ids:
                    raise UnknownCreditAccountsError(unknown_ids)

            await connection.execute("DELETE FROM plaid_spend.card_configs WHERE subject = $1", subject)
            if configuration.cards:
                await connection.executemany(
                    """
                    INSERT INTO plaid_spend.card_configs
                        (subject, account_id, label, limit_minor_units, alert_threshold_percent, enabled, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, now())
                    """,
                    [
                        (
                            subject,
                            card.account_id,
                            card.label,
                            card.limit_minor_units,
                            card.alert_threshold_percent,
                            card.enabled,
                        )
                        for card in configuration.cards
                    ],
                )
            await connection.execute("SELECT pg_notify($1, $2)", _CONFIG_CHANNEL, self._instance_id)
        self._wake_subscribers()

    async def settings_state(self, subject: str) -> tuple[tuple[AccountOption, ...], CardConfiguration]:
        accounts, configuration = await asyncio.gather(self.list_credit_accounts(), self.get_configuration(subject))
        return accounts, configuration

    async def read_view(self, subject: str) -> SpendView:
        pool = self._require_pool()
        generated_at = datetime.now(UTC)
        today = generated_at.date()
        async with pool.acquire() as connection:
            account_rows = await connection.fetch(
                """
                SELECT c.account_id, c.label, c.limit_minor_units, c.alert_threshold_percent,
                       a.name AS account_name, a.mask,
                       COALESCE(
                           a.iso_currency_code,
                           a.raw_json #>> '{balances,iso_currency_code}',
                           a.raw_json #>> '{balances,unofficial_currency_code}',
                           'USD'
                       ) AS currency,
                       l.institution_name,
                       l.item_id, l.last_synced_at
                FROM plaid_spend.card_configs AS c
                JOIN public.accounts AS a ON a.account_id = c.account_id
                JOIN public.links AS l ON l.item_id = a.item_id
                WHERE c.subject = $1 AND c.enabled IS TRUE
                  AND a.type = 'credit' AND l.status = 'active'
                ORDER BY c.account_id
                """,
                subject,
            )
            if not account_rows:
                return SpendView(generated_at=generated_at, cards=[])

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
            dated_account_rows = [row for row in account_rows if row["account_id"] in cycle_starts]
            transaction_rows: list[asyncpg.Record] = []
            if dated_account_rows:
                first_cycle_start = min(cycle_starts[row["account_id"]] for row in dated_account_rows)
                transaction_rows = await connection.fetch(
                    """
                    SELECT t.account_id, t.transaction_id, t.date, t.amount, t.pending,
                           t.pending_transaction_id,
                           COALESCE(t.iso_currency_code, t.raw_json->>'unofficial_currency_code') AS currency,
                           COALESCE(
                               t.pfc_detailed,
                               t.raw_json #>> '{personal_finance_category,detailed}'
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
                    [row["account_id"] for row in dated_account_rows],
                    first_cycle_start,
                    today,
                )

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
            if (cycle_start := cycle_starts.get(account_id)) is None:
                cards.append(
                    CardView(
                        account_id=account_id,
                        label=account["label"],
                        account_name=account["account_name"],
                        institution_name=account["institution_name"],
                        mask=account["mask"],
                        currency=account["currency"],
                        cycle_start=None,
                        spend_minor_units=None,
                        posted_minor_units=None,
                        pending_minor_units=None,
                        limit_minor_units=account["limit_minor_units"],
                        alert_threshold_percent=account["alert_threshold_percent"],
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
                if transaction["date"] < cycle_start or transaction["date"] > today:
                    continue
                if transaction["pending"] and (account_id, transaction["transaction_id"]) in superseded_pending_ids:
                    continue
                if transaction["pfc_detailed"] == _CARD_PAYMENT_CATEGORY:
                    continue
                if transaction["currency"] is not None and transaction["currency"] != account["currency"]:
                    continue
                amount_minor_units = _amount_minor_units(transaction["amount"], account["currency"])
                if transaction["pending"]:
                    pending_minor_units += amount_minor_units
                else:
                    posted_minor_units += amount_minor_units

            spend_minor_units = posted_minor_units + pending_minor_units
            limit_minor_units = account["limit_minor_units"]
            spend_percent = (
                float(Decimal(spend_minor_units) * Decimal(100) / Decimal(limit_minor_units))
                if limit_minor_units is not None
                else None
            )
            if spend_percent is not None and spend_percent >= 100:
                alert_state = AlertState.EXCEEDED
            elif (
                spend_percent is not None
                and account["alert_threshold_percent"] is not None
                and spend_percent >= account["alert_threshold_percent"]
            ):
                alert_state = AlertState.WARNING
            else:
                alert_state = AlertState.NORMAL
            cards.append(
                CardView(
                    account_id=account_id,
                    label=account["label"],
                    account_name=account["account_name"],
                    institution_name=account["institution_name"],
                    mask=account["mask"],
                    currency=account["currency"],
                    cycle_start=cycle_start,
                    spend_minor_units=spend_minor_units,
                    posted_minor_units=posted_minor_units,
                    pending_minor_units=pending_minor_units,
                    limit_minor_units=limit_minor_units,
                    alert_threshold_percent=account["alert_threshold_percent"],
                    spend_percent=spend_percent,
                    alert_state=alert_state,
                    last_synced_at=_as_utc(account["last_synced_at"]),
                    statement_available=True,
                )
            )
        return SpendView(generated_at=generated_at, cards=cards)

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
                await connection.add_listener(_CONFIG_CHANNEL, self._on_notification)
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

    def _on_notification(self, connection: asyncpg.Connection, pid: int, channel: str, payload: str) -> None:
        del connection, pid
        if channel == _CONFIG_CHANNEL and payload == self._instance_id:
            return
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


def _amount_minor_units(amount: float, currency: str) -> int:
    precision = get_currency_precision(currency)
    scale = 10**precision
    quantum = Decimal(1).scaleb(-precision)
    rounded = Decimal(str(amount)).quantize(quantum, rounding=ROUND_HALF_UP)
    return int(rounded * scale)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
