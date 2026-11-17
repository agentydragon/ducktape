"""Wire models for reconstructed recorded pace history."""

from datetime import date, datetime

from pydantic import BaseModel

from finance.plaid.spend.allowance import AnalysisCategory, PeriodId, Status


class PaceHistoryPoint(BaseModel):
    date: date
    observed_daily_minor_units: int | None


class PaceHistoryWindow(BaseModel):
    period_id: PeriodId
    points: list[PaceHistoryPoint]


class PaceHistoryDay(BaseModel):
    date: date
    categories: dict[str, int]
    coverage_complete: bool


class PaceHistory(BaseModel):
    generated_at: datetime
    time_zone: str
    currency: str
    status: Status
    last_synced_at: datetime | None
    daily_reference_minor_units: int
    windows: list[PaceHistoryWindow]
    issues: list[str]
    daily: list[PaceHistoryDay] = []
    categories: dict[str, AnalysisCategory] = {}
