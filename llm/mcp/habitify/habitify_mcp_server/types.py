"""
Type definitions for the Habitify MCP server.

These definitions are based on the actual Habitify API response structures
as documented in the reference YAML files.
"""

import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel


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

    unit_type: str
    value: float
    periodicity: str


class Area(BaseModel):
    """Model for habit area/category."""

    id: str
    name: str
    priority: Optional[str] = None


class Progress(BaseModel):
    """Model for habit progress information."""

    current_value: float
    target_value: float
    unit_type: str
    periodicity: str
    reference_date: str


class HabitStatus(BaseModel):
    """Model for habit status response from the API."""

    status: str

    # These fields aren't in the API response but are useful for our client
    date: Optional[str] = None  # Raw API response uses string dates
    value: Optional[float] = None
    note: Optional[str] = None

    # Model config to handle extra fields
    model_config = {"extra": "ignore"}


class HabitStatusResponse(BaseModel):
    """Client response model that uses Python date objects."""

    status: str
    date: Optional[datetime.date] = None  # Client response uses Python date objects
    value: Optional[float] = None
    note: Optional[str] = None

    @classmethod
    def from_api_model(
        cls, api_model: HabitStatus, date: Optional[str | datetime.date] = None
    ) -> "HabitStatusResponse":
        """Convert API model to client response model with Python date object."""
        # If we already have a date object, use it directly
        if isinstance(date, datetime.date):
            result_date = date
        # If we have a string date parameter, convert it
        elif isinstance(date, str):
            result_date = datetime.date.fromisoformat(date)
        # Otherwise use the API model's date string, which should always be present
        elif api_model.date:
            result_date = datetime.date.fromisoformat(api_model.date)
        # This should never happen as API always provides a date, but just in case
        else:
            raise ValueError("No date available in API model or parameters")

        return cls(
            status=api_model.status,
            date=result_date,
            value=api_model.value,
            note=api_model.note,
        )


class Habit(BaseModel):
    """Model for habit data from the API based on actual response structure."""

    id: str
    name: str
    is_archived: bool
    start_date: str
    time_of_day: list[str]
    goal: Optional[Goal] = None
    goal_history_items: list[Goal] = []
    log_method: str = ""
    recurrence: str
    remind: list[str] = []
    area: Optional[Area] = None
    created_date: str
    priority: float

    # Additional fields that appear in journal endpoint
    status: Optional[str] = None
    habit_type: Optional[int] = None
    progress: Optional[Progress] = None

    # Model config to handle extra fields
    model_config = {"extra": "ignore"}

    @property
    def archived(self) -> bool:
        """Return whether the habit is archived based on is_archived field."""
        return self.is_archived

    @property
    def category(self) -> Optional[str]:
        """Return the category/area name for compatibility."""
        if self.area:
            return self.area.name
        return None

    @property
    def goal_type(self) -> Optional[str]:
        """Extract goal type from goal for compatibility."""
        if self.goal:
            return self.goal.unit_type
        return None

    @property
    def target_value(self) -> Optional[float]:
        """Extract target value from goal for compatibility."""
        if self.goal:
            return self.goal.value
        return None


# Pydantic models for internal use - these provide proper type checking
class ResolvedHabit(BaseModel):
    """Data model for resolved habit information."""

    habit_id: str
    habit_name: Optional[str] = None
    match_type: Optional[str] = None


class ErrorResponse(BaseModel):
    """Model for structured error responses."""

    error: str
    category: Optional[str] = (
        None  # Error category (auth, not_found, validation, api, network, unknown)
    )
    matches: Optional[list[dict[str, str]]] = None
    total_matches: Optional[int] = None


class HabitsResult(BaseModel):
    """Result for getHabits tool."""

    habits: list[Habit]
    count: int


class HabitResult(BaseModel):
    """Result for getHabit tool."""

    habit: Habit


class StatusResult(BaseModel):
    """Result for checkHabit tool."""

    status: str
    date: str
    formatted_date: str
    completed: bool


class DateRangeStatusItem(BaseModel):
    """Status for a single date within a date range."""

    date: str
    formatted_date: str
    status: str
    completed: bool


class DateRangeStatusResult(BaseModel):
    """Result for getHabitStatus tool with date range."""

    statuses: list[DateRangeStatusItem]
    start_date: str
    end_date: str
    date_count: int


class LogResult(BaseModel):
    """Result for logHabit/setHabitStatus tool."""

    status: str
    date: str
    formatted_date: str
    note: Optional[str] = None
    value: Optional[float] = None


class UpdateResult(BaseModel):
    """Result for updateHabit tool."""

    habit: Habit
    changes: dict[str, Any]


class DeleteResult(BaseModel):
    """Result for deleteHabit tool."""

    deleted: bool = True


# Union type for all possible result types
ResultType = (
    HabitsResult
    | HabitResult
    | StatusResult
    | DateRangeStatusResult
    | LogResult
    | UpdateResult
    | DeleteResult
    | ErrorResponse
)
