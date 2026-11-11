"""Wire models for reconstructed recorded pace history."""

from datetime import date, datetime

from pydantic import BaseModel

from finance.plaid.spend.allowance import PeriodId, Status


class PaceHistoryPoint(BaseModel):
    date: date
    observed_daily_minor_units: int | None


class PaceHistoryWindow(BaseModel):
    period_id: PeriodId
    points: list[PaceHistoryPoint]


class PaceHistory(BaseModel):
    generated_at: datetime
    time_zone: str
    currency: str
    status: Status
    last_synced_at: datetime | None
    daily_reference_minor_units: int
    windows: list[PaceHistoryWindow]
    issues: list[str]
