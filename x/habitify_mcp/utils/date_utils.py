"""Date handling for Habitify requests and responses."""

from datetime import UTC, date, datetime, time


def aware_datetime(value: str | datetime) -> datetime:
    """Keep explicit offsets and interpret timezone-free values as UTC."""
    if not isinstance(value, (str, datetime)):
        raise ValueError(f"Expected datetime or ISO string, got {type(value)}")
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def target_datetime(value: str | date | None) -> datetime:
    """Resolve an optional request date once, before sending it to Habitify."""
    if value is None or value == "":
        return datetime.now(UTC)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=UTC)
    if isinstance(value, str):
        try:
            return aware_datetime(value)
        except ValueError:
            # Also accept dates with a single-digit month or day.
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return aware_datetime(value)
