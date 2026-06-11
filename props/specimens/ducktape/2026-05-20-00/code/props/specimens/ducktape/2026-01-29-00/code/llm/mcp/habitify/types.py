"""
Type definitions for the Habitify MCP server.

These definitions are based on the actual Habitify API response structures
as documented in the reference YAML files.
"""

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, PlainSerializer


def _parse_datetime(v: str | datetime) -> datetime:
    """Parse ISO datetime string or pass through datetime object."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v
    if isinstance(v, str):
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    raise ValueError(f"Expected datetime or ISO string, got {type(v)}")


def _serialize_datetime(v: datetime) -> str:
    """Serialize datetime to ISO string."""
    return v.isoformat()


# Annotated datetime type with proper serde for Habitify API
HabitifyDatetime = Annotated[
    datetime, BeforeValidator(_parse_datetime), PlainSerializer(_serialize_datetime, return_type=str)
]


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
    date: HabitifyDatetime | None = None
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
    date: HabitifyDatetime


class LogResult(BaseModel):
    """Result for logHabit/setHabitStatus tool."""

    status: Status
    date: HabitifyDatetime
    note: str | None = None
    value: float | None = None


class UpdateResult(BaseModel):
    """Result for updateHabit tool."""

    habit: Habit
    changes: dict[str, Any]


class DeleteResult(BaseModel):
    """Result for deleteHabit tool."""

    deleted: bool = True
