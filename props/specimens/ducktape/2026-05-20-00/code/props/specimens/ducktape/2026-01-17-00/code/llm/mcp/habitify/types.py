"""
Type definitions for the Habitify MCP server.

These definitions are based on the actual Habitify API response structures
as documented in the reference YAML files.
"""

import datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel


def _validate_iso_date(v: str) -> str:
    """Validate ISO 8601 date format (YYYY-MM-DD)."""
    if not isinstance(v, str):
        raise ValueError("Date must be a string")
    try:
        datetime.date.fromisoformat(v)
        return v
    except ValueError as e:
        raise ValueError(f"Invalid ISO date format, expected YYYY-MM-DD: {e}") from e


# Type alias for ISO 8601 date strings (YYYY-MM-DD)
ISODate = Annotated[str, AfterValidator(_validate_iso_date)]


class Status(str, Enum):
    """Valid habit status values."""

    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    NONE = "none"
    IN_PROGRESS = "in_progress"


class UnitType(str, Enum):
    """Valid unit types for habit goals."""

    REP = "rep"
    MIN = "min"
    HR = "hr"


class Periodicity(str, Enum):
    """Valid periodicity values for habit goals."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class TimeOfDay(str, Enum):
    """Valid time of day values."""

    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    ANY_TIME = "any_time"


class Goal(BaseModel):
    """Model for habit goal configuration."""

    unit_type: UnitType
    value: float
    periodicity: Periodicity


class Area(BaseModel):
    """Model for habit area/category."""

    id: str
    name: str
    priority: str | None = None


class Progress(BaseModel):
    """Model for habit progress information."""

    current_value: float
    target_value: float
    unit_type: UnitType
    periodicity: Periodicity
    reference_date: str


class HabitStatus(BaseModel):
    """Model for habit status response from the API."""

    status: Status
    date: ISODate | None = None
    value: float | None = None
    note: str | None = None

    # Model config to handle extra fields
    model_config = {"extra": "ignore"}


class Habit(BaseModel):
    """Model for habit data from the API based on actual response structure."""

    id: str
    name: str
    is_archived: bool
    start_date: str
    time_of_day: list[TimeOfDay]
    goal: Goal | None = None
    goal_history_items: list[Goal] = []
    log_method: str = ""
    recurrence: str
    remind: list[str] = []
    area: Area | None = None
    created_date: str
    priority: float

    # Additional fields that appear in journal endpoint
    status: Status | None = None
    habit_type: int | None = None
    progress: Progress | None = None

    # Model config to handle extra fields
    model_config = {"extra": "ignore"}

    @property
    def archived(self) -> bool:
        """Return whether the habit is archived based on is_archived field."""
        return self.is_archived

    @property
    def category(self) -> str | None:
        """Return the category/area name for compatibility."""
        if self.area:
            return self.area.name
        return None

    @property
    def goal_type(self) -> str | None:
        """Extract goal type from goal for compatibility."""
        if self.goal:
            return self.goal.unit_type
        return None

    @property
    def target_value(self) -> float | None:
        """Extract target value from goal for compatibility."""
        if self.goal:
            return self.goal.value
        return None


# Pydantic models for internal use - these provide proper type checking
class ResolvedHabit(BaseModel):
    """Data model for resolved habit information."""

    habit_id: str
    habit_name: str | None = None
    match_type: str | None = None


class HabitsResult(BaseModel):
    """Result for getHabits tool."""

    habits: list[Habit]
    count: int


class HabitResult(BaseModel):
    """Result for getHabit tool."""

    habit: Habit
    match_type: str | None = None


class StatusResult(BaseModel):
    """Result for checkHabit tool."""

    status: Status
    date: ISODate


class DateRangeStatusItem(BaseModel):
    """Status for a single date within a date range."""

    date: ISODate
    status: Status


class DateRangeStatusResult(BaseModel):
    """Result for getHabitStatus tool with date range."""

    statuses: list[DateRangeStatusItem]
    start_date: ISODate
    end_date: ISODate
    date_count: int


class LogResult(BaseModel):
    """Result for logHabit/setHabitStatus tool."""

    status: Status
    date: ISODate
    note: str | None = None
    value: float | None = None


class UpdateResult(BaseModel):
    """Result for updateHabit tool."""

    habit: Habit
    changes: dict[str, Any]


class DeleteResult(BaseModel):
    """Result for deleteHabit tool."""

    deleted: bool = True
