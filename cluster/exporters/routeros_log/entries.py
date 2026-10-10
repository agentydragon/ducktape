"""RouterOS log entries with absolute timestamps.

`/log/print` stamps each line in the device's local time, with as little of the date as RouterOS
thinks it needs: only the time for today, month and day for this year, the full date before that.
Each is resolved against the device's own clock (`/system/clock/print`), which also gives the UTC
offset. The offset is today's, so lines from before a DST change are off by its hour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_TIME = r"(?P<time>\d{2}:\d{2}:\d{2})"
_FORMATS = (
    re.compile(rf"{_TIME}"),
    re.compile(rf"(?P<month>\d{{2}})-(?P<day>\d{{2}}) {_TIME}"),
    re.compile(rf"(?P<year>\d{{4}})-(?P<month>\d{{2}})-(?P<day>\d{{2}}) {_TIME}"),
    re.compile(rf"(?P<month>[a-z]{{3}})/(?P<day>\d{{2}}) {_TIME}"),
    re.compile(rf"(?P<month>[a-z]{{3}})/(?P<day>\d{{2}})/(?P<year>\d{{4}}) {_TIME}"),
)


@dataclass(frozen=True)
class Clock:
    """The device's local date and UTC offset at the time of the poll."""

    today: date
    tz: timezone


@dataclass(frozen=True)
class LogEntry:
    id: str
    timestamp: datetime
    topics: str
    message: str


def _month(value: str) -> int:
    return int(value) if value.isdigit() else _MONTHS.index(value) + 1


def parse_date(value: str) -> date:
    """`2026-10-10`, or the pre-7.10 `oct/10/2026`."""
    if match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value):
        return date(int(match[1]), int(match[2]), int(match[3]))
    if match := re.fullmatch(r"([a-z]{3})/(\d{2})/(\d{4})", value):
        return date(int(match[3]), _month(match[1]), int(match[2]))
    raise ValueError(f"unrecognized RouterOS date {value=}")


def parse_offset(value: str) -> timezone:
    """`-07:00` or `+05:30`."""
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if match is None:
        raise ValueError(f"unrecognized RouterOS gmt-offset {value=}")
    minutes = int(match[2]) * 60 + int(match[3])
    return timezone(timedelta(minutes=-minutes if match[1] == "-" else minutes))


def parse_clock(reply: dict[str, str]) -> Clock:
    return Clock(today=parse_date(reply["date"]), tz=parse_offset(reply["gmt-offset"]))


def parse_timestamp(value: str, clock: Clock) -> datetime:
    for pattern in _FORMATS:
        if match := pattern.fullmatch(value):
            fields = match.groupdict()
            break
    else:
        raise ValueError(f"unrecognized RouterOS log time {value=}")
    day = clock.today
    if "month" in fields:
        day = date(int(fields.get("year") or clock.today.year), _month(fields["month"]), int(fields["day"]))
        # A month and day without a year that would be in the future is from last year.
        if "year" not in fields and day > clock.today:
            day = day.replace(year=day.year - 1)
    return datetime.combine(day, time.fromisoformat(fields["time"]), tzinfo=clock.tz).astimezone(UTC)


def parse_entry(reply: dict[str, str], clock: Clock) -> LogEntry:
    return LogEntry(
        id=reply[".id"],
        timestamp=parse_timestamp(reply["time"], clock),
        topics=reply["topics"],
        message=reply["message"],
    )
