"""Read the shared card configuration and compute statement-cycle spend from Plaid's mirror."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import anyio
import asyncpg
from babel.numbers import get_currency_precision
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance.plaid.db.models import async_session_factory
from finance.plaid.spend.allowance import (
    AccountingPolicy,
    AllowanceView,
    CreditTreatment,
    Disposition,
    EstimatePeriodId,
    ForecastView,
    Kind,
    PaceAlert,
    Period,
    PeriodId,
    Status,
    Transaction,
    TransactionDecision,
    TransactionPeriodId,
    accounting_disposition,
    calculate,
    daily_reference,
    prepare_accounting,
    round_minor_units,
)
from finance.plaid.spend.configuration import SpendConfiguration
from finance.plaid.spend.models import (
    AlertState,
    AllowanceConfigurationView,
    CardConfigurationView,
    CardView,
    PlaidTransactionDetails,
    ProvisionalCardPeriod,
    SpendConfigurationView,
    SpendTransactionsView,
    SpendView,
    StatementCycle,
    StatementReason,
    UnavailableCardPeriod,
)
from finance.plaid.spend.pace_history import PaceHistory, pace_history
from finance.plaid.spend.policy_queries import select_policy_accounts, select_policy_transactions
from finance.plaid.spend.portfolio import CoinbaseClient, read_portfolio
from finance.plaid.spend.portfolio_models import PortfolioView, SourceValuation
from finance.plaid.spend.reconciliation_views import allowance_view, sustainability_view
from finance.plaid.spend.statement_queries import (
    select_card_accounts,
    select_card_transactions,
    select_first_card_transactions,
    select_statement_snapshots,
)
from finance.plaid.spend.sustainability import (
    SustainabilityDashboard,
    SustainabilityView,
    calculate_sustainability,
    disabled_view,
    empty_portfolio,
)
from finance.plaid.spend.transaction_views import project_transactions

logger = logging.getLogger(__name__)

_CHANNEL = "plaid_spend_changed"


class SpendService:
    """Postgres reader and reconnecting NOTIFY subscriber for one shared card view."""

    def __init__(
        self,
        database_url: str,
        configuration: SpendConfiguration,
        *,
        dashboard_url: str,
        coinbase_client: CoinbaseClient | None = None,
    ) -> None:
        self._database_url = database_url
        self._configuration = configuration
        self._dashboard_url = dashboard_url
        self._coinbase_client = coinbase_client
        self._portfolio_lock = asyncio.Lock()
        self._portfolio_cache: tuple[datetime, PortfolioView] | None = None
        self._read_sessions: async_sessionmaker[AsyncSession] | None = None
        self._listening = asyncio.Event()
        self._revision = 0
        self._subscribers: set[asyncio.Queue[None]] = set()

    @property
    def listening(self) -> asyncio.Event:
        return self._listening

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def is_running(self) -> bool:
        return self._read_sessions is not None

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as resources:
            read_engine, read_sessions = async_session_factory(self._database_url, pool_size=1, max_overflow=7)
            resources.push_async_callback(read_engine.dispose)
            async with read_engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            self._read_sessions = read_sessions
            try:
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(self._listen_forever, name="plaid-spend-listener")
                    try:
                        yield
                    finally:
                        task_group.cancel_scope.cancel()
            finally:
                self._read_sessions = None

    def report_day(self, instant: datetime) -> date:
        return instant.astimezone(self._configuration.time_zone).date()

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
                forecast_basis_period_id=policy.forecast_basis_period_id,
                rules=policy.rules,
                reversal_pairs=policy.reversal_pairs,
                analysis_categories=policy.analysis_categories,
            )
        return SpendConfigurationView(
            time_zone=configuration.time_zone.key,
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
        estimate_period_id: EstimatePeriodId | None = None,
        allowance_decisions: list[TransactionDecision] | None = None,
        account_labels: dict[str, str] | None = None,
        card_transactions: list[Transaction] | None = None,
        statement_decisions: dict[tuple[str, str], tuple[StatementReason, int]] | None = None,
        transaction_details: dict[tuple[str, str], PlaidTransactionDetails] | None = None,
    ) -> SpendView:
        generated_at = datetime.now(UTC)
        today = self.report_day(generated_at)
        card_configs = {card.account_id: card for card in self._configuration.cards if card.enabled}
        if not card_configs:
            return SpendView(
                generated_at=generated_at,
                cards=[],
                allowance=await self._read_allowance(
                    generated_at, allowance_decisions, account_labels, transaction_details, estimate_period_id
                ),
                dashboard_url=self._dashboard_url,
            )

        async with self._require_read_sessions()() as session:
            account_rows = (await session.execute(select_card_accounts(list(card_configs)))).mappings().all()
            if account_rows:
                account_ids = [row["account_id"] for row in account_rows]
                liability_rows = (await session.execute(select_statement_snapshots(account_ids))).mappings().all()
                cycle_starts: dict[str, date] = {}
                for row in liability_rows:
                    if (cycle_start := _cycle_start(row["last_statement_issue_date"])) is not None:
                        cycle_starts[row["account_id"]] = cycle_start
                statement_account_ids = set(cycle_starts)
                missing_statement_ids = [
                    row["account_id"] for row in account_rows if row["account_id"] not in cycle_starts
                ]
                if missing_statement_ids:
                    first_transactions = (
                        (await session.execute(select_first_card_transactions(missing_statement_ids, today)))
                        .mappings()
                        .all()
                    )
                    cycle_starts.update({row["account_id"]: row["first_date"] for row in first_transactions})
                counted_accounts = [row for row in account_rows if row["account_id"] in cycle_starts]
                transaction_rows: Sequence[RowMapping] = ()
                if counted_accounts:
                    first_cycle_start = min(cycle_starts[row["account_id"]] for row in counted_accounts)
                    transaction_rows = (
                        (
                            await session.execute(
                                select_card_transactions(
                                    [row["account_id"] for row in counted_accounts],
                                    start_date=min(first_cycle_start, today - timedelta(days=29))
                                    if card_transactions is not None
                                    else first_cycle_start,
                                    through=today,
                                    include_history=card_transactions is not None,
                                    include_raw_json=transaction_details is not None,
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )

        if not account_rows:
            return SpendView(
                generated_at=generated_at,
                cards=[],
                allowance=await self._read_allowance(
                    generated_at, allowance_decisions, account_labels, transaction_details, estimate_period_id
                ),
                dashboard_url=self._dashboard_url,
            )

        transactions = [Transaction.model_validate(dict(row)) for row in transaction_rows]
        if card_transactions is not None:
            card_transactions.extend(transactions)
        if transaction_details is not None:
            transaction_details.update(
                {
                    (row["account_id"], row["transaction_id"]): _plaid_details(row["raw_json"])
                    for row in transaction_rows
                }
            )
        if account_labels is not None:
            account_labels.update({row["account_id"]: card_configs[row["account_id"]].label for row in account_rows})

        transactions_by_account: defaultdict[str, list[Transaction]] = defaultdict(list)
        for transaction in transactions:
            transactions_by_account[transaction.account_id].append(transaction)

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
                        statement_period=UnavailableCardPeriod(),
                        spend_minor_units=None,
                        posted_minor_units=None,
                        pending_minor_units=None,
                        limit_minor_units=card_config.limit_minor_units,
                        alert_threshold_percent=card_config.alert_threshold_percent,
                        spend_percent=None,
                        alert_state=AlertState.UNAVAILABLE,
                        last_synced_at=_as_utc(account["last_synced_at"]),
                    )
                )
                continue

            posted_minor_units = 0
            pending_minor_units = 0
            prepared = prepare_accounting(
                transactions_by_account.get(account_id, []),
                time_zone=self._configuration.time_zone,
                today=today,
                minor_unit_precision=get_currency_precision(account["currency"]),
            )
            accounting_policy = AccountingPolicy(
                currency=account["currency"],
                counted_kinds=frozenset(Kind),
                credits=CreditTreatment.ALL,
                honor_reversals=False,
                exclude_card_payments=True,
            )
            for entry in prepared.entries:
                transaction = entry.transaction
                key = (account_id, transaction.transaction_id)
                # Statements intentionally use posting dates, not purchase days.
                if not cycle_start <= transaction.date <= today:
                    if statement_decisions is not None:
                        statement_decisions[key] = (StatementReason.OUTSIDE_CYCLE, 0)
                    continue
                disposition = accounting_disposition(entry, accounting_policy)
                amount_minor_units = entry.amount_minor_units if disposition == Disposition.COUNTED else 0
                if statement_decisions is not None:
                    statement_decisions[key] = (StatementReason(disposition), amount_minor_units)
                if transaction.pending:
                    pending_minor_units += amount_minor_units
                else:
                    posted_minor_units += amount_minor_units

            spend_minor_units = posted_minor_units + pending_minor_units
            limit_minor_units = card_config.limit_minor_units
            statement_available = account_id in statement_account_ids
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
                    statement_period=StatementCycle(start=cycle_start, through=today)
                    if statement_available
                    else ProvisionalCardPeriod(start=cycle_start, through=today),
                    spend_minor_units=spend_minor_units,
                    posted_minor_units=posted_minor_units,
                    pending_minor_units=pending_minor_units,
                    limit_minor_units=limit_minor_units,
                    alert_threshold_percent=card_config.alert_threshold_percent,
                    spend_percent=spend_percent,
                    alert_state=alert_state,
                    last_synced_at=_as_utc(account["last_synced_at"]),
                )
            )
        return SpendView(
            generated_at=generated_at,
            cards=cards,
            allowance=await self._read_allowance(
                generated_at, allowance_decisions, account_labels, transaction_details, estimate_period_id
            ),
            dashboard_url=self._dashboard_url,
        )

    async def read_transactions(self, period: TransactionPeriodId = PeriodId.ROLLING_30D) -> SpendTransactionsView:
        decisions: list[TransactionDecision] = []
        labels: dict[str, str] = {}
        card_rows: list[Transaction] = []
        statement_decisions: dict[tuple[str, str], tuple[StatementReason, int]] = {}
        details_by_key: dict[tuple[str, str], PlaidTransactionDetails] = {}
        view = await self.read_view(
            allowance_decisions=decisions,
            account_labels=labels,
            card_transactions=card_rows,
            statement_decisions=statement_decisions,
            transaction_details=details_by_key,
        )
        return project_transactions(
            configuration=self._configuration,
            view=view,
            period=period,
            decisions=decisions,
            account_labels=labels,
            card_transactions=card_rows,
            statement_decisions=statement_decisions,
            transaction_details=details_by_key,
        )

    async def read_pace_history(self) -> PaceHistory | None:
        policy = self._configuration.allowance
        if policy is None:
            return None
        now = datetime.now(UTC)
        synced: list[datetime | None] = []
        try:
            async with self._require_read_sessions()() as session, session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                accounts = (
                    (await session.execute(select_policy_accounts(sorted(policy.spending_account_ids))))
                    .mappings()
                    .all()
                )
                synced = [_as_utc(row["last_synced_at"]) for row in accounts]
                valid = len(accounts) == len(policy.spending_account_ids) and all(
                    row["type"] in ("credit", "depository")
                    and value is not None
                    and now - value <= timedelta(hours=policy.max_sync_age_hours)
                    for row, value in zip(accounts, synced, strict=True)
                )
                if valid:
                    rows = (
                        (
                            await session.execute(
                                select_policy_transactions(sorted(policy.spending_account_ids), include_raw_json=False)
                            )
                        )
                        .mappings()
                        .all()
                    )
                    evidence = self._configuration.sustainability
                    return pace_history(
                        policy,
                        [Transaction.model_validate(dict(row)) for row in rows],
                        time_zone=self._configuration.time_zone,
                        now=now,
                        last_synced_at=min(value for value in synced if value is not None),
                        account_history=evidence.account_history if evidence else {},
                    )
        except SQLAlchemyError:
            logger.warning("Could not read pace history from the spending source")
        return PaceHistory(
            generated_at=now,
            time_zone=self._configuration.time_zone.key,
            currency=policy.currency,
            status=Status.UNAVAILABLE,
            last_synced_at=min((value for value in synced if value is not None), default=None),
            daily_reference_minor_units=round_minor_units(daily_reference(policy.monthly_minor_units), precision=0),
            windows=[],
            issues=["Spending account coverage or freshness is unavailable."],
        )

    async def _read_allowance(
        self,
        now: datetime,
        decisions: list[TransactionDecision] | None = None,
        account_labels: dict[str, str] | None = None,
        transaction_details: dict[tuple[str, str], PlaidTransactionDetails] | None = None,
        estimate_period_id: EstimatePeriodId | None = None,
    ) -> AllowanceView | None:
        policy = self._configuration.allowance
        if policy is None:
            return None
        # No account IDs or names leave the server in the allowance view.
        async with self._require_read_sessions()() as session:
            accounts = (
                (await session.execute(select_policy_accounts(sorted(policy.spending_account_ids)))).mappings().all()
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
                    daily_reference_minor_units=round_minor_units(
                        daily_reference(policy.monthly_minor_units), precision=0
                    ),
                    activation_at=policy.activation_at,
                    time_zone=self._configuration.time_zone.key,
                    available_minor_units=None,
                    next_credit_at=None,
                    posted_minor_units=0,
                    pending_minor_units=0,
                    review_minor_units=0,
                    review_transaction_count=0,
                    unmatched_refunds_minor_units=0,
                    spend_periods=[],
                    recorded_pace_periods=[],
                    forecast=ForecastView(
                        basis_period=Period.for_id(
                            estimate_period_id or policy.forecast_basis_period_id, self.report_day(now)
                        ),
                        daily_pace_minor_units=None,
                        projected_cycle_end_minor_units=None,
                        estimated_exhaustion_at=None,
                        alert_state=PaceAlert.UNAVAILABLE,
                    ),
                    spending_signal=PaceAlert.UNAVAILABLE,
                    last_synced_at=last_synced,
                    note="Account coverage or sync freshness unavailable; do not rely on the allowance.",
                )
            rows: Sequence[RowMapping] = ()
            today = self.report_day(now)
            if policy.activation_at <= today:
                rows = (
                    (
                        await session.execute(
                            select_policy_transactions(
                                sorted(policy.spending_account_ids), include_raw_json=transaction_details is not None
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                # A posting-date bound can drop late postings or purchases that
                # cross midnight in the policy zone; calculate filters after
                # choosing each transaction's effective purchase day.
            if transaction_details is not None:
                transaction_details.update(
                    {(row["account_id"], row["transaction_id"]): _plaid_details(row["raw_json"]) for row in rows}
                )
        return allowance_view(
            calculate(
                policy,
                [Transaction.model_validate(dict(row)) for row in rows],
                time_zone=self._configuration.time_zone,
                now=now,
                last_synced_at=last_synced,
                decisions=decisions,
                estimate_period_id=estimate_period_id,
            )
        )

    async def _read_current_portfolio(self, now: datetime) -> PortfolioView:
        policy = self._configuration.sustainability
        if policy is None:
            return empty_portfolio()
        async with self._portfolio_lock:
            cached = self._portfolio_cache
            if cached is not None and now - cached[0] < timedelta(seconds=60):
                return cached[1]
            try:
                view = await read_portfolio(
                    self._require_read_sessions(), policy.portfolio, now=now, coinbase_client=self._coinbase_client
                )
            except SQLAlchemyError:
                view = PortfolioView(
                    status="unavailable",
                    net_minor_units=None,
                    assets_minor_units=None,
                    liabilities_minor_units=None,
                    excluded_minor_units=None,
                    sources=[
                        SourceValuation(
                            name="plaid",
                            status="unavailable",
                            amount_minor_units=None,
                            as_of=None,
                            issues=["The Plaid mirror could not be read."],
                        )
                    ],
                )
            if view.status == "ready":
                self._portfolio_cache = (now, view)
            return view

    async def read_sustainability(self, window_days: int | None = None) -> SustainabilityView:
        policy = self._configuration.sustainability
        if policy is not None and window_days is not None and window_days not in policy.display.windows_days:
            raise ValueError("window must be configured")
        days = window_days if window_days is not None else policy.display.default_window_days if policy else 90
        return (await self._read_sustainability_windows([days]))[0]

    async def read_sustainability_dashboard(self) -> SustainabilityDashboard:
        policy = self._configuration.sustainability
        return SustainabilityDashboard(
            default_window_days=policy.display.default_window_days if policy else 90,
            windows=await self._read_sustainability_windows(policy.display.windows_days if policy else [90]),
        )

    async def _read_sustainability_windows(self, windows_days: list[int]) -> list[SustainabilityView]:
        now = datetime.now(UTC)
        policy = self._configuration.sustainability
        allowance = self._configuration.allowance
        if policy is None or allowance is None:
            return [disabled_view(now, self._configuration.time_zone.key, self.report_day(now))]
        issues = []
        transactions: list[Transaction] = []
        account_labels = dict(self._configuration.account_labels)
        account_labels.update({card.account_id: card.label for card in self._configuration.cards})
        try:
            async with self._require_read_sessions()() as session, session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                accounts = (
                    (await session.execute(select_policy_accounts(sorted(allowance.spending_account_ids))))
                    .mappings()
                    .all()
                )
                for row in accounts:
                    if row["institution_name"]:
                        account_labels.setdefault(row["account_id"], f"{row['institution_name']} {row['type']}")
                if len(accounts) != len(allowance.spending_account_ids) or any(
                    row["type"] not in ("credit", "depository")
                    or row["last_synced_at"] is None
                    or now - row["last_synced_at"] > timedelta(hours=allowance.max_sync_age_hours)
                    for row in accounts
                ):
                    issues.append("Spending account coverage or sync freshness is unavailable.")
                rows = (
                    (
                        await session.execute(
                            select_policy_transactions(sorted(allowance.spending_account_ids), include_raw_json=False)
                        )
                    )
                    .mappings()
                    .all()
                )
                transactions = [Transaction.model_validate(dict(row)) for row in rows]
        except SQLAlchemyError:
            issues.append("The spending source could not be read; totals are unavailable.")
        portfolio = await self._read_current_portfolio(now)
        return [
            sustainability_view(
                calculate_sustainability(
                    policy,
                    allowance,
                    transactions,
                    portfolio,
                    time_zone=self._configuration.time_zone,
                    now=now,
                    window_days=days,
                    source_issues=issues,
                    account_labels=account_labels,
                )
            )
            for days in windows_days
        ]

    def _require_read_sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._read_sessions is None:
            raise RuntimeError("spend read session factory is not initialized")
        return self._read_sessions

    async def _listen_forever(self) -> None:
        while True:
            connection: asyncpg.Connection | None = None
            try:
                connection = await asyncpg.connect(self._database_url)
                terminated = asyncio.Event()
                connection.add_termination_listener(lambda _, event=terminated: event.set())
                await connection.add_listener(_CHANNEL, self._on_notification)
                self._listening.set()
                self._wake_subscribers()
                await terminated.wait()
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as error:
                logger.warning("Postgres spend listener disconnected (%s); reconnecting", type(error).__name__)
            finally:
                if self._listening.is_set():
                    self._listening.clear()
                    self._wake_subscribers()
                if connection is not None:
                    await connection.close(timeout=2)
            await asyncio.sleep(3)

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
