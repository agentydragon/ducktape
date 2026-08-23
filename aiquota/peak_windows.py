"""Provider peak-burn schedules — periods where quota drains faster than 1x.

Vendors publish these as wall-clock ranges in their own timezone: z.ai's
"14:00-18:00" is Singapore time, named on the plan notice but dropped nearly
everywhere the window is quoted, and useless to convert in your head besides.
Pinning the zone here is the point of the module — callers get UTC instants and
render them in the viewer's local zone.

Schedules are vendor policy and change without notice; they are data, kept in
`SCHEDULES` at the bottom, not logic.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel

# A weekday-only schedule's longest off-peak stretch is Friday evening to Monday
# morning. Scanning eight days always spans at least one such gap, and leaves
# room to list several upcoming windows.
_SCAN_DAYS = 8


@dataclass(frozen=True)
class PeakPeriod:
    """One recurring wall-clock range, in the owning schedule's timezone.

    `end` earlier than `start` means the range wraps past midnight, and the
    post-midnight half belongs to the weekday the range started on.
    """

    weekdays: frozenset[int]  # 0 = Monday, matching date.weekday()
    start: time
    end: time


@dataclass(frozen=True)
class BurnSchedule:
    multiplier: float
    tz: ZoneInfo
    periods: tuple[PeakPeriod, ...]
    applies_to: str
    source: str
    """Where the hours, weekdays, multiplier and timezone were read from.

    Vendors restate these without changelogs, so a schedule that cannot be
    re-checked against its origin is a liability. Anyone editing the numbers is
    expected to move the URL with them.
    """


class PeakInterval(BaseModel):
    """One concrete occurrence of a peak period, as absolute instants."""

    start: datetime
    end: datetime


class BurnStatus(BaseModel):
    """Current burn state plus the schedule ahead, for the CLI and GNOME popup.

    Instants stay absolute; renderers localize. The service that serves this
    over HTTP runs in a UTC container, so localizing here would silently hand
    every remote client UTC.
    """

    in_peak: bool
    multiplier: float
    peak_multiplier: float
    upcoming: list[PeakInterval]
    applies_to: str

    @property
    def changes_at(self) -> datetime:
        """When the multiplier next flips — the current window's end, else the next start."""
        first = self.upcoming[0]
        return first.end if self.in_peak else first.start


def _occurrences(schedule: BurnSchedule, first_day: date) -> list[PeakInterval]:
    """Expand the recurring periods into concrete intervals across the horizon."""
    out: list[PeakInterval] = []
    for offset in range(_SCAN_DAYS):
        day = first_day + timedelta(days=offset)
        for period in schedule.periods:
            if day.weekday() not in period.weekdays:
                continue
            # A wrapped range ends on the following calendar day.
            end_day = day if period.start <= period.end else day + timedelta(days=1)
            out.append(
                PeakInterval(
                    start=datetime.combine(day, period.start, tzinfo=schedule.tz),
                    end=datetime.combine(end_day, period.end, tzinfo=schedule.tz),
                )
            )
    return sorted(out, key=lambda interval: interval.start)


def upcoming_peaks(schedule: BurnSchedule, now: datetime, count: int = 3) -> list[PeakInterval]:
    """The current peak window (if any) followed by the next ones, soonest first.

    Starts the scan a day early so a wrapped window that began yesterday and is
    still running gets reported as current rather than skipped.
    """
    first_day = now.astimezone(schedule.tz).date() - timedelta(days=1)
    return [interval for interval in _occurrences(schedule, first_day) if interval.end > now][:count]


def in_peak(schedule: BurnSchedule, now: datetime) -> bool:
    """Start is inclusive, end exclusive, so adjacent windows share no instant."""
    return any(interval.start <= now < interval.end for interval in upcoming_peaks(schedule, now, count=1))


def status(schedule: BurnSchedule, now: datetime, count: int = 3) -> BurnStatus:
    upcoming = upcoming_peaks(schedule, now, count)
    if not upcoming:
        raise ValueError(f"schedule yields no upcoming window: {schedule=}")
    peak = upcoming[0].start <= now < upcoming[0].end
    return BurnStatus(
        in_peak=peak,
        multiplier=schedule.multiplier if peak else 1.0,
        peak_multiplier=schedule.multiplier,
        upcoming=upcoming,
        applies_to=schedule.applies_to,
    )


def status_for(provider: str, now: datetime, count: int = 3) -> BurnStatus | None:
    schedule = SCHEDULES.get(provider)
    return status(schedule, now, count) if schedule else None


_WEEKDAYS = frozenset(range(5))

SCHEDULES: dict[str, BurnSchedule] = {
    # Hours, weekdays and the 3x coefficient are all stated in the plan-revision
    # notice. The notice writes the zone as "Singapore Standard Time"; elsewhere
    # z.ai writes the same window as "14:00-18:00 UTC+8" and in places omits the
    # zone entirely, which is what makes this worth encoding rather than reading
    # off the page each time. SGT is UTC+8 year-round with no DST, so the two
    # spellings agree.
    "zai": BurnSchedule(
        multiplier=3.0,
        tz=ZoneInfo("Asia/Singapore"),
        periods=(PeakPeriod(weekdays=_WEEKDAYS, start=time(14, 0), end=time(18, 0)),),
        applies_to="GLM-5.3, GLM-5-Turbo",
        source="https://docs.z.ai/devpack/notice/usage-revision",
    )
}
