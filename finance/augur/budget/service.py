"""Budget snapshot service: returns SQL-backed Plaid budget read models.

The service holds the plaid mirror session factory (constructed once at server startup,
shared across requests) so every endpoint reuses the same asyncpg connection pool. SSL
handshake + pool init cost ~500ms over the cluster port-forward; doing it per request
was the dominant latency in the early budget endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance.augur.budget.csv_export import build_summary_csv, build_transactions_csv
from finance.augur.budget.schema import BudgetConfig
from finance.augur.budget.sql_read_model import read_budget_bucket_transactions, read_budget_snapshot
from finance.augur.budget.wire import (
    BudgetAdjustment,
    BudgetSnapshotResponse,
    BudgetTransactionsResponse,
    TrailingMonthsWindow,
    WindowSpec,
)


def _resolve_window(window: WindowSpec, *, today: date, coverage_starts: date | None) -> date:
    """Map a wire `WindowSpec` to the window's start (the actual first covered day).

    For `TrailingMonthsWindow`, walks back N calendar months from the month containing `today`.
    For `CoverageWindow`, anchors at `coverage_starts` (raises if the deployment has none).
    In either case, the start is clamped to `coverage_starts` -- months before that are partial
    cross-account coverage and would skew family totals. The window always ends at `today`.
    """
    current_month = date(today.year, today.month, 1)
    if isinstance(window, TrailingMonthsWindow):
        total = current_month.year * 12 + (current_month.month - 1) - (window.months - 1)
        start_month = date(total // 12, total % 12 + 1, 1)
    else:
        if coverage_starts is None:
            raise ValueError(
                "since_coverage_start window requested but no coverage_starts is configured "
                "for this deployment; either configure BudgetSourceConfig.coverage_starts or "
                "pick a trailing-months window"
            )
        start_month = date(coverage_starts.year, coverage_starts.month, 1)
    if coverage_starts is not None and start_month < coverage_starts:
        start_month = coverage_starts
    return start_month


@dataclass(frozen=True)
class BudgetService:
    config: BudgetConfig
    session_factory: async_sessionmaker[AsyncSession]

    async def build_snapshot(self, *, window: WindowSpec) -> BudgetSnapshotResponse:
        today = date.today()
        coverage_starts = self.config.source.coverage_starts
        start_month = _resolve_window(window, today=today, coverage_starts=coverage_starts)
        return await read_budget_snapshot(
            session_factory=self.session_factory,
            config=self.config,
            window_start=start_month,
            window_end=today,
            account_ids=self.config.source.plaid_account_ids,
        )

    async def build_snapshot_csv(self, *, window: WindowSpec, adjustments: dict[str, BudgetAdjustment]) -> str:
        bucket_ids = {bucket.id for bucket in self.config.buckets}
        unknown = sorted(set(adjustments) - bucket_ids)
        if unknown:
            raise ValueError(f"unknown budget adjustment bucket_id(s): {unknown}")
        return build_summary_csv(await self.build_snapshot(window=window), adjustments)

    async def list_transactions_in_bucket(self, *, bucket_id: str, window: WindowSpec) -> BudgetTransactionsResponse:
        today = date.today()
        coverage_starts = self.config.source.coverage_starts
        start_month = _resolve_window(window, today=today, coverage_starts=coverage_starts)
        return await read_budget_bucket_transactions(
            session_factory=self.session_factory,
            config=self.config,
            bucket_id=bucket_id,
            window_start=start_month,
            window_end=today,
            account_ids=self.config.source.plaid_account_ids,
        )

    async def build_transactions_csv(self, *, bucket_id: str, window: WindowSpec) -> str:
        return build_transactions_csv(await self.list_transactions_in_bucket(bucket_id=bucket_id, window=window))
