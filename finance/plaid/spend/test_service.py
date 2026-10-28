"""Spend-service integration tests against a migrated PostgreSQL testcontainer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError

from finance.plaid.spend import service
from finance.plaid.spend.allowance import (
    AllOf,
    AllowancePolicy,
    AmountExact,
    AnalysisCategory,
    CategoryExact,
    DateRange,
    Disposition,
    Kind,
    NameContains,
    PeriodId,
    ReversalPair,
    Rule,
    RuleScope,
    Status,
)
from finance.plaid.spend.app import _event_stream
from finance.plaid.spend.configuration import CardConfig, SpendConfiguration
from finance.plaid.spend.models import AlertState, AnalysisCategoryView, SpendTransactionRow
from finance.plaid.spend.portfolio_models import (
    HoldingValueCorrection,
    PlaidSourcePolicy,
    PortfolioPolicy,
    PortfolioSources,
    PortfolioView,
    SourceValuation,
)
from finance.plaid.spend.service import SpendService
from finance.plaid.spend.sustainability import SustainabilityDisplay, SustainabilityPolicy


def excluded_row(
    amount: int,
    *,
    currency: str = "USD",
    pending: bool = False,
    disposition: Disposition | None = Disposition.EXCLUDED,
    category: str | None = "transfer",
) -> SpendTransactionRow:
    return SpendTransactionRow(
        date=date(2026, 1, 1),
        account_label="Example account",
        name="Synthetic flow",
        merchant_name=None,
        amount_minor_units=amount,
        currency=currency,
        pending=pending,
        allowance_in_scope=True,
        disposition=disposition,
        allowance_minor_units=0,
        pace_effects=[],
        statement_minor_units=None,
        statement_reason=None,
        pfc_primary=None,
        pfc_detailed=None,
        merchant_category_code=None,
        category=AnalysisCategoryView(id=category, label=category.title(), color="#64748B") if category else None,
    )


def test_excluded_flows_keep_directions_and_currencies_separate() -> None:
    rows = [excluded_row(500), excluded_row(-500), excluded_row(0, pending=True), excluded_row(700, currency="EUR")]
    groups = service.summarize_excluded_flows(rows)
    assert [
        (g.currency, g.debit_minor_units, g.credit_minor_units, g.transaction_count, g.pending_count) for g in groups
    ] == [("EUR", 700, 0, 1, 0), ("USD", 500, 500, 3, 1)]
    assert groups[0].category.id == "transfer"


@pytest.mark.parametrize("disposition", [value for value in Disposition if value != Disposition.EXCLUDED] + [None])
def test_excluded_flows_ignore_other_dispositions(disposition: Disposition | None) -> None:
    assert service.summarize_excluded_flows([excluded_row(500, disposition=disposition)]) == []


def test_excluded_flows_keep_categories_and_uncategorized_rows() -> None:
    groups = service.summarize_excluded_flows(
        [excluded_row(100), excluded_row(200, category="tax"), excluded_row(-50, category=None)]
    )
    assert [(g.category.id, g.debit_minor_units, g.credit_minor_units) for g in groups] == [
        ("tax", 200, 0),
        ("transfer", 100, 0),
        ("uncategorized", 0, 50),
    ]
    assert service.summarize_excluded_flows([]) == []


@pytest_asyncio.fixture
async def connection(postgres_url: str) -> AsyncGenerator[asyncpg.Connection]:
    conn = await asyncpg.connect(postgres_url)
    try:
        yield conn
    finally:
        await conn.close()


async def add_link(connection: asyncpg.Connection, item_id: str, *, synced: datetime) -> None:
    await connection.execute(
        """INSERT INTO public.links
           (item_id, institution_name, products_requested, products_authorized, products_billed,
            status, access_token_secret, last_synced_at, created_at, updated_at)
           VALUES ($1, 'Example Bank', '[]'::json, '[]'::json, '[]'::json, 'active', 'example-secret', $2, $2, $2)""",
        item_id,
        synced,
    )


async def add_account(connection: asyncpg.Connection, account_id: str, item_id: str, *, type: str) -> None:
    await connection.execute(
        """INSERT INTO public.accounts (account_id, item_id, name, mask, type, iso_currency_code, raw_json, updated_at)
           VALUES ($1, $2, $3, '1234', $4, 'USD', '{}'::json, NOW())""",
        account_id,
        item_id,
        f"Example {account_id}",
        type,
    )


async def add_liability(connection: asyncpg.Connection, account_id: str, item_id: str, issue: date) -> None:
    await connection.execute(
        """INSERT INTO public.liability_credit_snapshots (account_id, item_id, captured_at, raw_json)
           VALUES ($1, $2, $3, $4::json)""",
        account_id,
        item_id,
        datetime.now(UTC),
        json.dumps({"last_statement_issue_date": issue.isoformat()}),
    )


async def add_transaction(
    connection: asyncpg.Connection,
    account_id: str,
    item_id: str,
    transaction_id: str,
    on_date: date,
    amount: Decimal,
    *,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    category: str | None = None,
    currency: str = "USD",
    raw_json: dict[str, object] | None = None,
) -> None:
    await connection.execute(
        """INSERT INTO public.transactions
           (transaction_id, account_id, item_id, date, amount, name, merchant_name, pending,
            pending_transaction_id, pfc_primary, pfc_detailed, iso_currency_code, removed, raw_json, updated_at)
           VALUES ($1, $2, $3, $4, $5, 'EXAMPLE SHOP', NULL, $6, $7, 'SHOPPING', $8, $9, false, $10::json, NOW())""",
        transaction_id,
        account_id,
        item_id,
        on_date,
        amount,
        pending,
        pending_transaction_id,
        category,
        currency,
        json.dumps(raw_json or {}),
    )


async def test_purchase_days_match_allowance_and_list_without_changing_statement_dates(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    now = datetime.now(UTC)
    zone = ZoneInfo("America/Los_Angeles")
    today = now.astimezone(zone).date()
    activation = today - timedelta(days=10)
    await add_link(connection, "item-time", synced=now)
    await add_account(connection, "card-time", "item-time", type="credit")
    await add_liability(connection, "card-time", "item-time", activation - timedelta(days=1))
    # A future posting date must not hide a purchase already authorized locally.
    await add_transaction(
        connection,
        "card-time",
        "item-time",
        "late-post",
        today + timedelta(days=2),
        Decimal("7.00"),
        raw_json={"authorized_datetime": now.isoformat()},
    )
    # A posted charge authorized before activation informs pace but not allowance debt.
    await add_transaction(
        connection,
        "card-time",
        "item-time",
        "prior-purchase",
        today,
        Decimal("11.00"),
        raw_json={"authorized_date": (activation - timedelta(days=1)).isoformat()},
    )
    await add_transaction(connection, "card-time", "item-time", "date-only", today, Decimal("9.00"))
    await add_transaction(
        connection,
        "card-time",
        "item-time",
        "pending-time",
        today,
        Decimal("5.00"),
        pending=True,
        raw_json={"authorized_datetime": now.isoformat()},
    )
    await add_transaction(
        connection,
        "card-time",
        "item-time",
        "replacement-time",
        today,
        Decimal("6.00"),
        pending_transaction_id="pending-time",
        raw_json={"authorized_datetime": now.isoformat()},
    )
    config = SpendConfiguration(
        time_zone=zone,
        cards=[CardConfig(account_id="card-time", label="Example card", enabled=True)],
        allowance=AllowancePolicy(
            monthly_minor_units=10_000,
            activation_at=activation,
            spending_account_ids={"card-time"},
            analysis_categories={"unclassified": AnalysisCategory(label="Unclassified", color="#D97706")},
            rules=[Rule(condition=CategoryExact(field="pfc_primary", value="SHOPPING"), kind=Kind.FLEXIBLE)],
        ),
    )
    service = SpendService(postgres_url, config, dashboard_url="https://spend.example.test")
    async with service.lifespan():
        view = await service.read_view()
        transactions = await service.read_transactions(period=PeriodId.CREDIT_CYCLE)
        rolling = await service.read_transactions(period=PeriodId.ROLLING_30D)
    assert view.allowance is not None
    assert view.allowance.posted_minor_units == 2_200
    assert view.allowance.pending_minor_units == 0
    assert view.cards[0].posted_minor_units == 2_600  # original posting dates: 11 + 9 + 6
    assert view.cards[0].pending_minor_units == 0  # posted replacement supersedes pending
    assert transactions.summary.net_allowance_spend_minor_units == 2_200
    assert all(row.date == today for row in transactions.rows)
    late_post = next(row for row in transactions.rows if row.amount_minor_units == 700)
    assert late_post.plaid_date == today + timedelta(days=2)
    assert rolling.summary.net_allowance_spend_minor_units == 2_200
    assert len(rolling.rows) == 5
    assert min(row.date for row in rolling.rows) == activation - timedelta(days=1)


async def test_read_view_uses_statement_cycle_and_normalizes_transactions(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    today = datetime.now(UTC).date()
    early_cycle_start = today - timedelta(days=10)
    late_cycle_start = today - timedelta(days=3)
    await add_link(connection, "item-1", synced=datetime.now(UTC))
    for account_id, start in (("card-early", early_cycle_start), ("card-late", late_cycle_start)):
        await add_account(connection, account_id, "item-1", type="credit")
        await add_liability(connection, account_id, "item-1", start - timedelta(days=1))
    # The SQL query uses the earliest selected cycle. Python still applies the later card boundary.
    for transaction_id, on_date, amount in (
        ("before-cycle", late_cycle_start - timedelta(days=1), Decimal("90.00")),
        ("cycle-start", late_cycle_start, Decimal("12.34")),
        ("refund", today, Decimal("-3.00")),
        ("pending-only", today, Decimal("7.50")),
        ("pending-old", today, Decimal("20.00")),
        ("posted-replacement", today, Decimal("22.00")),
        ("card-payment", today, Decimal("100.00")),
        ("other-currency", today, Decimal("40.00")),
        ("future", today + timedelta(days=1), Decimal("60.00")),
    ):
        await add_transaction(
            connection,
            "card-late",
            "item-1",
            transaction_id,
            on_date,
            amount,
            pending=transaction_id in ("pending-only", "pending-old"),
            pending_transaction_id="pending-old" if transaction_id == "posted-replacement" else None,
            category="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT" if transaction_id == "card-payment" else None,
            currency="CAD" if transaction_id == "other-currency" else "USD",
        )
    configuration = SpendConfiguration(
        time_zone=ZoneInfo("UTC"),
        cards=[
            CardConfig(account_id=aid, label=aid, limit_minor_units=100_000, enabled=True)
            for aid in ("card-early", "card-late")
        ],
    )
    service = SpendService(postgres_url, configuration, dashboard_url="https://spend.example.test")
    async with service.lifespan():
        view = await service.read_view()
    early_card, late_card = view.cards
    assert early_card.statement_period.kind == "statement"
    assert early_card.statement_period.start == early_cycle_start
    assert early_card.spend_minor_units == 0
    assert late_card.statement_period.kind == "statement"
    assert late_card.statement_period.start == late_cycle_start
    assert late_card.posted_minor_units == 3_134
    assert late_card.pending_minor_units == 750
    assert late_card.spend_minor_units == 3_884
    assert late_card.spend_percent == Decimal("3.884")
    assert late_card.model_dump(mode="json")["spend_percent"] == "3.884"


async def test_card_without_statement_reports_observed_spend_not_a_statement_cycle(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    today = datetime.now(UTC).date()
    start = today - timedelta(days=32)
    await add_link(connection, "item-new", synced=datetime.now(UTC))
    for account_id in ("card-first", "card-empty"):
        await add_account(connection, account_id, "item-new", type="credit")
    await add_transaction(connection, "card-first", "item-new", "first", start, Decimal("10.00"))
    await add_transaction(connection, "card-first", "item-new", "pending", today, Decimal("15.00"), pending=True)
    await add_transaction(
        connection, "card-first", "item-new", "posted", today, Decimal("17.00"), pending_transaction_id="pending"
    )
    await add_transaction(connection, "card-first", "item-new", "new", today, Decimal("12.00"))
    await add_transaction(
        connection,
        "card-first",
        "item-new",
        "repayment",
        today,
        Decimal("30.00"),
        category="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )
    service = SpendService(
        postgres_url,
        SpendConfiguration(
            time_zone=ZoneInfo("UTC"),
            cards=[
                CardConfig(account_id=aid, label=aid, limit_minor_units=10_000, enabled=True)
                for aid in ("card-first", "card-empty")
            ],
        ),
        dashboard_url="https://spend.example.test",
    )
    async with service.lifespan():
        view = await service.read_view()
    cards = {card.account_id: card for card in view.cards}
    first, empty = cards["card-first"], cards["card-empty"]
    assert first.statement_period.kind == "provisional"
    assert first.statement_period.start == start  # first observed transaction, not a statement boundary
    assert first.posted_minor_units == 3_900
    assert first.pending_minor_units == 0  # superseded by posted
    assert first.spend_minor_units == 3_900
    assert first.spend_percent is None
    assert first.alert_state == AlertState.UNAVAILABLE
    assert empty.statement_period.kind == "unavailable"
    assert empty.spend_minor_units is None


def test_report_day_uses_configured_zone_for_card_and_allowance_dates() -> None:
    config = SpendConfiguration(
        time_zone=ZoneInfo("America/Los_Angeles"),
        cards=[],
        allowance=AllowancePolicy(
            monthly_minor_units=10_000,
            activation_at=date(2026, 10, 5),
            spending_account_ids={"example-card"},
            analysis_categories={"unclassified": AnalysisCategory(label="Unclassified", color="#D97706")},
            rules=[Rule(condition=CategoryExact(field="pfc_primary", value="SHOPPING"), kind=Kind.FLEXIBLE)],
        ),
    )
    service = SpendService("unused", config, dashboard_url="https://spend.example.test")
    assert service.report_day(datetime(2026, 10, 9, 6, 59, tzinfo=UTC)) == date(2026, 10, 8)
    assert service.report_day(datetime(2026, 10, 9, 7, tzinfo=UTC)) == date(2026, 10, 9)
    allowance_config = service.read_configuration().allowance
    assert allowance_config is not None
    assert service.read_configuration().time_zone == "America/Los_Angeles"
    assert all(rule.id is None for rule in allowance_config.rules)


@pytest.mark.asyncio
async def test_report_day_and_disabled_sustainability_use_zone_without_allowance() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    service = SpendService(
        "unused", SpendConfiguration(time_zone=zone, cards=[]), dashboard_url="https://spend.example.test"
    )

    assert service.report_day(datetime(2026, 10, 9, 6, 59, tzinfo=UTC)) == date(2026, 10, 8)
    disabled = await service.read_sustainability()

    assert disabled.status == "disabled"
    assert disabled.time_zone == zone.key
    dashboard = await service.read_sustainability_dashboard()
    assert dashboard.windows[0].status == "disabled"
    assert dashboard.windows[0].time_zone == zone.key


def test_configuration_view_lists_one_off_rules_without_account_ids() -> None:
    config = SpendConfiguration(
        time_zone=ZoneInfo("UTC"),
        cards=[],
        allowance=AllowancePolicy(
            monthly_minor_units=10_000,
            activation_at=date(2026, 10, 5),
            spending_account_ids={"example-card"},
            analysis_categories={"unclassified": AnalysisCategory(label="Unclassified", color="#D97706")},
            rules=[
                Rule(
                    condition=AllOf(
                        conditions=[
                            NameContains(field="name", substring="EXAMPLE FUND"),
                            DateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
                        ]
                    ),
                    kind=Kind.EXCLUDED,
                    scope=RuleScope.ONE_OFF,
                    id="example-transfer-leg",
                    description="Own-account transfer leg; confirmed by the owner 2026-10-09.",
                ),
                Rule(condition=CategoryExact(field="pfc_primary", value="SHOPPING"), kind=Kind.FLEXIBLE),
            ],
        ),
    )
    service = SpendService("unused", config, dashboard_url="https://spend.example.test")
    allowance = service.read_configuration().allowance
    assert allowance is not None
    assert [rule.id for rule in allowance.rules] == ["example-transfer-leg", None]
    applied = allowance.rules[0]
    assert applied.scope == RuleScope.ONE_OFF
    assert isinstance(applied.condition, AllOf)
    name_condition = applied.condition.conditions[0]
    assert isinstance(name_condition, NameContains)
    assert name_condition.substring == "EXAMPLE FUND"


async def test_allowance_account_coverage_and_freshness_gate(connection: asyncpg.Connection, postgres_url: str) -> None:
    now = datetime.now(UTC)
    midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    for account_id, item_id, type in (("card-1", "item-card", "credit"), ("checking-1", "item-checking", "depository")):
        await add_link(connection, item_id, synced=now)
        await add_account(connection, account_id, item_id, type=type)
    await add_transaction(connection, "card-1", "item-card", "purchase", now.date(), Decimal("12.00"))
    config = SpendConfiguration(
        time_zone=ZoneInfo("UTC"),
        cards=[],
        allowance=AllowancePolicy(
            monthly_minor_units=10_000,
            activation_at=midnight.date(),
            spending_account_ids={"card-1", "checking-1"},
            analysis_categories={"unclassified": AnalysisCategory(label="Unclassified", color="#D97706")},
            rules=[Rule(condition=CategoryExact(field="pfc_primary", value="SHOPPING"), kind=Kind.FLEXIBLE)],
        ),
    )
    service = SpendService(postgres_url, config, dashboard_url="https://spend.example.test")
    async with service.lifespan():
        result = await service.read_view()
        assert result.allowance is not None
        assert result.allowance.available_minor_units == 8_800
        assert result.allowance.daily_reference_minor_units == 329
        await connection.execute("UPDATE public.links SET status = 'removed' WHERE item_id = 'item-checking'")
        unavailable = (await service.read_view()).allowance
        assert unavailable is not None
        assert unavailable.status == Status.UNAVAILABLE
        assert unavailable.daily_reference_minor_units == 329
        await connection.execute(
            "UPDATE public.links SET status = 'active', last_synced_at = $1 WHERE item_id = 'item-checking'",
            now - timedelta(days=4),
        )
        unavailable = (await service.read_view()).allowance
        assert unavailable is not None
        assert unavailable.status == Status.UNAVAILABLE
        assert unavailable.daily_reference_minor_units == 329


async def test_prior_purchases_are_queried_for_pace_but_not_balance(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    now = datetime.now(UTC)
    await add_link(connection, "item-card", synced=now)
    await add_account(connection, "card-1", "item-card", type="credit")
    await add_transaction(connection, "card-1", "item-card", "prior", now.date() - timedelta(days=1), Decimal("70.00"))
    await add_transaction(connection, "card-1", "item-card", "older", now.date() - timedelta(days=20), Decimal("30.00"))
    config = SpendConfiguration(
        time_zone=ZoneInfo("UTC"),
        cards=[],
        allowance=AllowancePolicy(
            monthly_minor_units=10_000,
            activation_at=now.date(),
            spending_account_ids={"card-1"},
            analysis_categories={"unclassified": AnalysisCategory(label="Unclassified", color="#D97706")},
            rules=[Rule(condition=CategoryExact(field="pfc_primary", value="SHOPPING"), kind=Kind.FLEXIBLE)],
        ),
    )

    service = SpendService(postgres_url, config, dashboard_url="https://spend.example.test")
    async with service.lifespan():
        allowance = (await service.read_view()).allowance
    assert allowance is not None
    assert allowance.available_minor_units == 10_000
    observed = {report.period.id: report.observed_daily_minor_units for report in allowance.recorded_pace_periods}
    assert observed[PeriodId.ROLLING_7D] == 1_000
    assert observed[PeriodId.ROLLING_30D] == 10_000 // 30
    assert allowance.forecast.daily_pace_minor_units == 1_000
    assert (
        next(report.spend_minor_units for report in allowance.spend_periods if report.period.id == PeriodId.ROLLING_7D)
        == 0
    )


async def test_reversal_uses_old_retained_original_and_preserves_card_statement(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    now = datetime.now(UTC)
    today = now.date()
    original_day = today - timedelta(days=60)
    reversal_day = today - timedelta(days=2)
    await add_link(connection, "item-reversal", synced=now)
    await add_account(connection, "card-reversal", "item-reversal", type="credit")
    await add_liability(connection, "card-reversal", "item-reversal", today - timedelta(days=10))
    await add_transaction(connection, "card-reversal", "item-reversal", "original", original_day, Decimal("80.00"))
    await add_transaction(connection, "card-reversal", "item-reversal", "return", reversal_day, Decimal("-80.00"))
    await add_transaction(connection, "card-reversal", "item-reversal", "replacement", reversal_day, Decimal("90.00"))
    pair = ReversalPair(
        id="example-return",
        description="Synthetic full return with a separately counted replacement.",
        original=AllOf(conditions=[DateRange(start=original_day, end=original_day), AmountExact(value="80")]),
        reversal=AllOf(conditions=[DateRange(start=reversal_day, end=reversal_day), AmountExact(value="-80")]),
    )
    config = SpendConfiguration(
        time_zone=ZoneInfo("UTC"),
        cards=[CardConfig(account_id="card-reversal", label="Example card", enabled=True)],
        allowance=AllowancePolicy(
            monthly_minor_units=100_000,
            activation_at=today - timedelta(days=20),
            spending_account_ids={"card-reversal"},
            analysis_categories={"unclassified": AnalysisCategory(label="Unclassified", color="#777777")},
            rules=[Rule(condition=CategoryExact(field="pfc_primary", value="SHOPPING"), kind=Kind.FLEXIBLE)],
            reversal_pairs=[pair],
        ),
    )
    spend_service = SpendService(postgres_url, config, dashboard_url="https://spend.example.test")
    async with spend_service.lifespan():
        view = await spend_service.read_view()
        transactions = await spend_service.read_transactions(period=PeriodId.ROLLING_30D)
    assert view.allowance is not None
    assert view.allowance.posted_minor_units == 9_000
    assert view.allowance.unmatched_refunds_minor_units == 0
    assert view.allowance.note is None
    assert view.cards[0].posted_minor_units == 1_000  # Statement still sees the actual -80 credit and +90 debit.
    assert len(transactions.rows) == 2  # Original is outside the displayed period, but still reconciled.
    credit = next(row for row in transactions.rows if row.amount_minor_units < 0)
    assert credit.disposition == Disposition.REVERSED
    assert credit.reversal_pair == pair
    assert credit.effective_kind == Kind.EXCLUDED
    assert credit.rule is not None
    assert credit.rule.kind == Kind.FLEXIBLE
    assert credit.statement_minor_units == -8_000
    assert credit.allowance_minor_units == 0
    configuration = spend_service.read_configuration()
    assert configuration.allowance is not None
    assert configuration.allowance.reversal_pairs == [pair]
    assert "card-reversal" not in configuration.model_dump_json()


def _sustainability_configuration(today: date, spending_account_id: str) -> SpendConfiguration:
    zone = ZoneInfo("America/Los_Angeles")
    return SpendConfiguration(
        time_zone=zone,
        cards=[],
        allowance=AllowancePolicy(
            monthly_minor_units=10_000,
            activation_at=today - timedelta(days=730),
            spending_account_ids={spending_account_id},
            analysis_categories={
                "unclassified": AnalysisCategory(label="Unclassified", color="#D97706"),
                "fixed": AnalysisCategory(label="Required", color="#0F766E"),
                "flexible": AnalysisCategory(label="Flexible", color="#7C3AED"),
            },
            rules=[
                Rule(
                    condition=CategoryExact(field="pfc_detailed", value="FIXED"),
                    kind=Kind.FIXED,
                    analysis_category="fixed",
                ),
                Rule(
                    condition=CategoryExact(field="pfc_detailed", value="FLEXIBLE"),
                    kind=Kind.FLEXIBLE,
                    analysis_category="flexible",
                ),
            ],
        ),
        sustainability=SustainabilityPolicy(
            display=SustainabilityDisplay(windows_days=[30, 90], default_window_days=90),
            portfolio=PortfolioPolicy(
                sources=PortfolioSources(
                    plaid=PlaidSourcePolicy(asset_account_ids=["synthetic-private-portfolio-source-id"])
                )
            ),
        ),
    )


def _synthetic_portfolio() -> PortfolioView:
    observed_at = datetime.now(UTC)
    return PortfolioView(
        status="ready",
        net_minor_units=50_000_000,
        assets_minor_units=55_000_000,
        liabilities_minor_units=5_000_000,
        excluded_minor_units=0,
        sources=[
            SourceValuation(
                name="Synthetic portfolio source",
                status="ready",
                amount_minor_units=55_000_000,
                as_of=observed_at,
                issues=[],
            )
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["postgres_error", "unavailable_result"])
async def test_portfolio_read_retries_failure_and_caches_ready_result(
    monkeypatch: pytest.MonkeyPatch, failure_mode: str
) -> None:
    now = datetime.now(UTC)
    spend_service = SpendService(
        "unused",
        _sustainability_configuration(now.date(), "synthetic-spending-account"),
        dashboard_url="https://spend.example.test",
    )
    monkeypatch.setattr(spend_service, "_require_portfolio_sessions", object)
    attempts = 0

    async def read_portfolio(
        _session_factory: object, _policy: PortfolioPolicy, *, now: datetime, coinbase_client: object | None
    ) -> PortfolioView:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure_mode == "postgres_error":
                raise SQLAlchemyError("synthetic transient database failure")
            return PortfolioView(
                status="unavailable",
                net_minor_units=None,
                assets_minor_units=None,
                liabilities_minor_units=None,
                excluded_minor_units=None,
                sources=[
                    SourceValuation(
                        name="coinbase",
                        status="unavailable",
                        amount_minor_units=None,
                        as_of=None,
                        issues=["Coinbase could not provide a current valuation."],
                    )
                ],
            )
        return _synthetic_portfolio()

    monkeypatch.setattr(service, "read_portfolio", read_portfolio)

    unavailable = await spend_service._read_current_portfolio(now)
    recovered = await spend_service._read_current_portfolio(now)
    cached = await spend_service._read_current_portfolio(now)

    assert unavailable.status == "unavailable"
    assert len(unavailable.sources) == 1
    assert unavailable.sources[0].name == ("plaid" if failure_mode == "postgres_error" else "coinbase")
    assert unavailable.sources[0].status == "unavailable"
    assert unavailable.sources[0].issues
    assert recovered.status == "ready"
    assert cached.status == "ready"
    assert attempts == 2


@pytest.mark.asyncio
async def test_portfolio_reads_complete_snapshots_through_sqlalchemy(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    now = datetime.now(UTC)
    captured_at = now - timedelta(minutes=2)
    previous_capture_at = captured_at - timedelta(hours=1)
    await add_link(connection, "portfolio-item", synced=captured_at)
    await add_account(connection, "portfolio-brokerage", "portfolio-item", type="investment")
    await connection.execute(
        """INSERT INTO public.balance_snapshots
           (account_id, item_id, captured_at, current, iso_currency_code)
           VALUES ($1, $2, $3, 900, 'USD'), ($1, $2, $4, 1100, 'USD')""",
        "portfolio-brokerage",
        "portfolio-item",
        captured_at - timedelta(minutes=1),
        captured_at + timedelta(minutes=1),
    )
    for security_id in ("portfolio-sold", "portfolio-held", "portfolio-zero"):
        await connection.execute(
            """INSERT INTO public.securities
               (security_id, name, type, iso_currency_code, raw_json, updated_at)
               VALUES ($1, $1, 'equity', 'USD', '{}'::json, $2)""",
            security_id,
            captured_at,
        )

    async def add_holding_capture(capture_time: datetime, current: int) -> int:
        api_event_id = await connection.fetchval(
            """INSERT INTO public.plaid_api_events
               (endpoint, item_id, status, request_json, response_json, created_at)
               VALUES ('investments/holdings/get', $1, 'ok', '{}'::jsonb, '{}'::jsonb, $2)
               RETURNING id""",
            "portfolio-item",
            capture_time,
        )
        capture_id: int = await connection.fetchval(
            """INSERT INTO public.holding_captures (item_id, captured_at, api_event_id)
               VALUES ($1, $2, $3) RETURNING id""",
            "portfolio-item",
            capture_time,
            api_event_id,
        )
        await connection.execute(
            """INSERT INTO public.holding_capture_accounts
               (capture_id, account_id, item_id, current, iso_currency_code)
               VALUES ($1, 'portfolio-brokerage', 'portfolio-item', $2, 'USD')""",
            capture_id,
            current,
        )
        return capture_id

    previous_capture_id = await add_holding_capture(previous_capture_at, 1500)
    capture_id = await add_holding_capture(captured_at, 1000)
    for security_id, holdings_time, value, row_capture_id in (
        ("portfolio-sold", previous_capture_at, 500, previous_capture_id),
        ("portfolio-held", captured_at, 1000, capture_id),
        ("portfolio-zero", captured_at, 0, capture_id),
    ):
        await connection.execute(
            """INSERT INTO public.holding_snapshots
               (account_id, security_id, item_id, captured_at, institution_value, iso_currency_code, raw_json,
                capture_id)
               VALUES ($1, $2, $3, $4, $5, 'USD', '{}'::json, $6)""",
            "portfolio-brokerage",
            security_id,
            "portfolio-item",
            holdings_time,
            value,
            row_capture_id,
        )

    await add_link(connection, "revoked-portfolio-item", synced=captured_at)
    await add_account(connection, "revoked-portfolio-account", "revoked-portfolio-item", type="investment")
    await connection.execute("UPDATE public.links SET status = 'revoked' WHERE item_id = 'revoked-portfolio-item'")
    await connection.execute(
        """INSERT INTO public.balance_snapshots
           (account_id, item_id, captured_at, current, iso_currency_code)
           VALUES ('revoked-portfolio-account', 'revoked-portfolio-item', $1, 500, 'USD')""",
        captured_at,
    )

    policy = PortfolioPolicy(
        sources=PortfolioSources(
            plaid=PlaidSourcePolicy(
                asset_account_ids=["portfolio-brokerage"],
                holding_value_corrections=[
                    HoldingValueCorrection(
                        account_id="portfolio-brokerage",
                        security_ids=["portfolio-held", "portfolio-zero"],
                        value_multiplier=Decimal(1),
                    )
                ],
            )
        )
    )
    config = _sustainability_configuration(now.date(), "synthetic-spending-account")
    sustainability = config.sustainability
    assert sustainability is not None
    config = config.model_copy(update={"sustainability": sustainability.model_copy(update={"portfolio": policy})})
    spend_service = SpendService(postgres_url, config, dashboard_url="https://spend.example.test")
    async with spend_service.lifespan():
        view = await spend_service._read_current_portfolio(now)
        revoked_view = await service.read_portfolio(
            spend_service._require_portfolio_sessions(),
            PortfolioPolicy(
                sources=PortfolioSources(plaid=PlaidSourcePolicy(asset_account_ids=["revoked-portfolio-account"]))
            ),
            now=now,
        )

    assert view.status == "ready"
    assert view.assets_minor_units == 100_000
    assert view.excluded_minor_units == 0
    assert view.sources[0].as_of == captured_at
    assert revoked_view.status == "unavailable"
    assert "missing or inactive" in revoked_view.sources[0].issues[0]
    assert not spend_service.is_running


@pytest.mark.parametrize("label_source", ["institution", "private", "card"])
async def test_read_sustainability_uses_postgres_transactions_for_each_window(
    connection: asyncpg.Connection, postgres_url: str, monkeypatch: pytest.MonkeyPatch, label_source: str
) -> None:
    now = datetime.now(UTC)
    zone = ZoneInfo("America/Los_Angeles")
    today = now.astimezone(zone).date()
    yesterday = today - timedelta(days=1)
    spending_account_id = "synthetic-private-spending-account-id"
    item_id = "synthetic-spending-item"
    await add_link(connection, item_id, synced=now)
    await add_account(connection, spending_account_id, item_id, type="credit")
    # One old source record establishes that this account has transaction history.
    await add_transaction(
        connection,
        spending_account_id,
        item_id,
        "synthetic-history-start",
        date(today.year - 1, 1, 1),
        Decimal("999.00"),
        category="FIXED",
    )
    await add_transaction(
        connection,
        spending_account_id,
        item_id,
        "synthetic-90-day",
        today - timedelta(days=40),
        Decimal("20.00"),
        category="FIXED",
    )
    await add_transaction(
        connection,
        spending_account_id,
        item_id,
        "synthetic-outside-30",
        today - timedelta(days=31),
        Decimal("7.00"),
        category="FLEXIBLE",
    )
    await add_transaction(
        connection,
        spending_account_id,
        item_id,
        "synthetic-window-boundary",
        today - timedelta(days=30),
        Decimal("3.00"),
        category="FIXED",
    )
    await add_transaction(
        connection,
        spending_account_id,
        item_id,
        "synthetic-pending",
        yesterday,
        Decimal("100.00"),
        pending=True,
        category="FLEXIBLE",
    )
    await add_transaction(
        connection,
        spending_account_id,
        item_id,
        "synthetic-posted-replacement",
        yesterday,
        Decimal("12.00"),
        pending_transaction_id="synthetic-pending",
        category="FLEXIBLE",
    )
    await add_transaction(
        connection, spending_account_id, item_id, "synthetic-flexible", yesterday, Decimal("10.00"), category="FLEXIBLE"
    )
    await add_transaction(connection, spending_account_id, item_id, "synthetic-provisional", yesterday, Decimal("5.00"))
    await add_transaction(
        connection, spending_account_id, item_id, "synthetic-current-day", today, Decimal("999.00"), category="FIXED"
    )
    config = _sustainability_configuration(today, spending_account_id)
    expected_label = "Example Bank credit"
    if label_source == "private":
        expected_label = "Synthetic selected spending account"
        config = config.model_copy(update={"account_labels": {spending_account_id: expected_label}})
    elif label_source == "card":
        expected_label = "Synthetic configured card"
        config = config.model_copy(
            update={"cards": [CardConfig(account_id=spending_account_id, label=expected_label, enabled=False)]}
        )
    service = SpendService(postgres_url, config, dashboard_url="https://spend.example.test")

    portfolio_reads = 0

    async def read_portfolio(_now: datetime) -> PortfolioView:
        nonlocal portfolio_reads
        portfolio_reads += 1
        return _synthetic_portfolio()

    monkeypatch.setattr(service, "_read_current_portfolio", read_portfolio)
    async with service.lifespan():
        default = await service.read_sustainability()
        alternative = await service.read_sustainability(30)
        portfolio_reads = 0
        dashboard = await service.read_sustainability_dashboard()
        assert portfolio_reads == 1

    assert default.status == "ready"
    assert default.window_days == 90
    assert default.covered_days == 90
    assert default.period_start == today - timedelta(days=90)
    assert default.period_end == yesterday
    assert default.mandatory_minor_units == 2_300
    assert default.flexible_minor_units == 2_900
    assert default.provisional_minor_units == 500
    assert default.spending_minor_units == 5_700
    assert default.history[-1].date == yesterday
    assert default.history[-1].covered_days == 90
    assert len(default.account_coverage) == 1
    assert default.account_coverage[0].account_label == expected_label
    assert default.account_coverage[0].first_recorded_on == date(today.year - 1, 1, 1)
    assert default.account_coverage[0].unknown_days_in_window == 0

    assert alternative.status == "ready"
    assert alternative.window_days == 30
    assert alternative.covered_days == 30
    assert alternative.period_start == today - timedelta(days=30)
    assert alternative.period_end == yesterday
    assert alternative.mandatory_minor_units == 300
    assert alternative.flexible_minor_units == 2_200
    assert alternative.provisional_minor_units == 500
    assert alternative.spending_minor_units == 3_000
    assert alternative.history[-1].covered_days == 30
    assert dashboard.default_window_days == 90
    assert [window.window_days for window in dashboard.windows] == [30, 90]
    assert {window.generated_at for window in dashboard.windows} == {dashboard.windows[0].generated_at}
    assert dashboard.windows[0].portfolio == dashboard.windows[1].portfolio
    assert dashboard.windows[0].spending_minor_units == alternative.spending_minor_units
    assert dashboard.windows[1].spending_minor_units == default.spending_minor_units
    assert dashboard.windows[0].history == alternative.history
    assert dashboard.windows[1].history == default.history
    serialized = dashboard.model_dump_json()
    assert spending_account_id not in serialized
    assert "synthetic-private-portfolio-source-id" not in serialized


@pytest.mark.parametrize("source_state", ["stale", "inactive"])
async def test_read_sustainability_fails_closed_for_unusable_spending_source(
    connection: asyncpg.Connection, postgres_url: str, monkeypatch: pytest.MonkeyPatch, source_state: str
) -> None:
    now = datetime.now(UTC)
    zone = ZoneInfo("America/Los_Angeles")
    today = now.astimezone(zone).date()
    account_id = "synthetic-unusable-spending-account-id"
    item_id = f"synthetic-{source_state}-spending-item"
    synced = now - timedelta(days=4) if source_state == "stale" else now
    await add_link(connection, item_id, synced=synced)
    await add_account(connection, account_id, item_id, type="credit")
    await add_transaction(
        connection,
        account_id,
        item_id,
        "synthetic-source-history",
        date(today.year - 1, 1, 1),
        Decimal("25.00"),
        category="FIXED",
    )
    await add_transaction(
        connection,
        account_id,
        item_id,
        "synthetic-recent-charge",
        today - timedelta(days=1),
        Decimal("10.00"),
        category="FLEXIBLE",
    )
    if source_state == "inactive":
        await connection.execute("UPDATE public.links SET status = 'removed' WHERE item_id = $1", item_id)
    config = _sustainability_configuration(today, account_id)
    service = SpendService(postgres_url, config, dashboard_url="https://spend.example.test")

    async def read_portfolio(_now: datetime) -> PortfolioView:
        return _synthetic_portfolio()

    monkeypatch.setattr(service, "_read_current_portfolio", read_portfolio)
    async with service.lifespan():
        result = await service.read_sustainability()

    assert result.status == "unavailable"
    assert result.annualized_spending_minor_units is None
    assert result.spending_draw_rate is None
    assert "Spending account coverage or sync freshness is unavailable." in result.issues
    if source_state == "stale":
        assert result.spending_minor_units == 1_000
    else:
        assert result.covered_days == 0
        assert result.spending_minor_units == 0


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


async def test_event_stream_sends_initial_view_and_notification_update(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    today = datetime.now(UTC).date()
    await add_link(connection, "item-1", synced=datetime.now(UTC))
    await add_account(connection, "card-1", "item-1", type="credit")
    await add_liability(connection, "card-1", "item-1", today - timedelta(days=1))
    await add_transaction(connection, "card-1", "item-1", "purchase", today, Decimal("10.00"))
    service = SpendService(
        postgres_url,
        SpendConfiguration(
            time_zone=ZoneInfo("UTC"), cards=[CardConfig(account_id="card-1", label="Test", enabled=True)]
        ),
        dashboard_url="https://spend.example.test",
    )
    async with service.lifespan():
        await asyncio.wait_for(service.listening.wait(), timeout=10)
        async with aclosing(_event_stream(cast(Any, _ConnectedRequest()), service)) as stream:
            initial = json.loads((await asyncio.wait_for(anext(stream), timeout=10)).partition("data: ")[2])
            assert initial["cards"][0]["spend_minor_units"] == 1_000
            await connection.execute("UPDATE public.transactions SET amount = 25 WHERE transaction_id = 'purchase'")
            await connection.execute("SELECT pg_notify('plaid_spend_changed', '')")
            updated = json.loads((await asyncio.wait_for(anext(stream), timeout=10)).partition("data: ")[2])
            assert updated["cards"][0]["spend_minor_units"] == 2_500
    assert not service._subscribers


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
