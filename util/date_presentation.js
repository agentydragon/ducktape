const DEADLINE_DATE_ONLY_AFTER_MS = 14 * 24 * 60 * 60 * 1000;

function validInstant(value) {
  if (value == null || value === "") return null;
  const instant = value instanceof Date ? value : new Date(value);
  return Number.isNaN(instant.getTime()) ? null : instant;
}

export function formatLocalClock(instant) {
  return instant.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

export function formatLocalWeekday(instant) {
  return instant.toLocaleDateString("en-US", { weekday: "short" });
}

export function formatLocalTimestamp(value, now = new Date(), details = false) {
  const instant = validInstant(value);
  if (!instant) return null;
  if (details) {
    const zone =
      new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
        .formatToParts(instant)
        .find((part) => part.type === "timeZoneName")?.value ?? "";
    const year = instant.getFullYear();
    const month = String(instant.getMonth() + 1).padStart(2, "0");
    const day = String(instant.getDate()).padStart(2, "0");
    return `${year}-${month}-${day} ${formatLocalClock(instant)} ${zone}`;
  }

  const label = instant.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(instant.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
  return Math.abs(instant.getTime() - now.getTime()) <= DEADLINE_DATE_ONLY_AFTER_MS
    ? `${label}, ${formatLocalClock(instant)}`
    : label;
}

export function formatCalendarDate(value, now = new Date()) {
  if (typeof value !== "string") return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);

  // Interpret this as a calendar date in UTC, never as an instant in the viewer's zone.
  const date = new Date(Date.UTC(2000, month - 1, day, 12));
  date.setUTCFullYear(year);
  if (date.getUTCFullYear() !== year || date.getUTCMonth() + 1 !== month || date.getUTCDate() !== day) return null;

  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    ...(year !== now.getFullYear() ? { year: "numeric" } : {}),
    timeZone: "UTC",
  }).format(date);
}

export function formatPeakInterval(interval) {
  const start = new Date(interval.start);
  return `${formatLocalWeekday(start)} ${formatLocalClock(start)}-${formatLocalClock(new Date(interval.end))}`;
}
