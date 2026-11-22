import { rrulestr } from "rrule";
import { z } from "zod";

const date = z.iso.date().nullish();
const dateTime = z.iso.datetime({ offset: true, local: true }).nullish();
const timeZone = z.string().min(1).nullish();

export interface EventTime {
  date: string | undefined;
  dateTime: string | undefined;
  timeZone: string | undefined;
}

export const eventTime: z.ZodType<EventTime> = z
  .strictObject({ date, date_time: dateTime, dateTime, time_zone: timeZone, timeZone })
  .transform((value) => ({
    date: value.date ?? undefined,
    dateTime: value.date_time ?? value.dateTime ?? undefined,
    timeZone: value.time_zone ?? value.timeZone ?? undefined,
  }))
  .refine((value) => (value.date === undefined) !== (value.dateTime === undefined));

const DAY_MS = 24 * 60 * 60 * 1000;
const SHORT_DATE = { weekday: "short", month: "short", day: "numeric", year: "numeric" } as const;
const SHORT_TIME = { hour: "numeric", minute: "2-digit" } as const;
const utcDate = new Intl.DateTimeFormat("en-US", { ...SHORT_DATE, timeZone: "UTC" });

interface TimedParts {
  day: string;
  time: string;
  zone?: string;
  offset?: string;
}

function offsetAt(formatter: Intl.DateTimeFormat, instant: number): number | undefined {
  const name = formatter.formatToParts(instant).find((part) => part.type === "timeZoneName")?.value;
  if (name === "GMT") return 0;
  const parts = name?.match(/^GMT([+-])(\d\d):(\d\d)(?::(\d\d))?$/);
  if (!parts) return undefined;
  const [, sign, hours, minutes, seconds] = parts;
  return (sign === "-" ? -1 : 1) * (Number(hours) * 3600 + Number(minutes) * 60 + Number(seconds ?? 0)) * 1000;
}

function resolveInstant(value: string, zone?: string): number | undefined {
  if (/(?:Z|[+-]\d\d:\d\d)$/.test(value)) {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  if (!zone) return undefined;
  const wall = Date.parse(`${value}Z`);
  if (!Number.isFinite(wall)) return undefined;
  try {
    const formatter = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "longOffset" });
    const offsets = new Set([wall - DAY_MS, wall, wall + DAY_MS].map((instant) => offsetAt(formatter, instant)));
    const matches = Array.from(offsets).flatMap((offset) => {
      if (offset === undefined) return [];
      const instant = wall - offset;
      return offsetAt(formatter, instant) === offset ? [instant] : [];
    });
    // A clock transition can make a submitted wall time ambiguous or nonexistent.
    return matches.length === 1 ? matches[0] : undefined;
  } catch {
    return undefined;
  }
}

function timedParts(value: string, zone: string | undefined, viewerZone: string): TimedParts | undefined {
  const instant = resolveInstant(value, zone);
  const viewerLocal = instant !== undefined;
  const displayed = instant ?? Date.parse(`${value}Z`);
  if (!Number.isFinite(displayed)) return undefined;
  try {
    const timeZone = viewerLocal ? viewerZone : "UTC";
    const parts = new Intl.DateTimeFormat("en-US", {
      ...SHORT_TIME,
      timeZone,
      ...(viewerLocal ? { timeZoneName: "shortOffset" } : {}),
    }).formatToParts(displayed);
    return {
      day: new Intl.DateTimeFormat("en-US", { ...SHORT_DATE, timeZone }).format(displayed),
      time: parts
        .filter((part) => part.type !== "timeZoneName")
        .map((part) => part.value)
        .join("")
        .trim()
        .replace(/\s+/g, " "),
      zone: viewerLocal ? undefined : (zone ?? "timezone unspecified"),
      offset: parts.find((part) => part.type === "timeZoneName")?.value,
    };
  } catch {
    return undefined;
  }
}

function viewerTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

function formatDateTime(value: string, zone: string | undefined, viewerZone: string): string {
  const parts = timedParts(value, zone, viewerZone);
  if (parts) return [parts.day, parts.time, parts.zone].filter(Boolean).join(" · ");
  return zone ? `${value} · ${zone}` : value;
}

function timeWithOffset(parts: TimedParts): string {
  return parts.offset ? `${parts.time} ${parts.offset}` : parts.time;
}

export function formatEventTime(value: EventTime, viewerZone = viewerTimeZone()): string {
  if (value.date !== undefined) return utcDate.format(new Date(value.date));
  return value.dateTime === undefined ? "" : formatDateTime(value.dateTime, value.timeZone, viewerZone);
}

export function formatEventRange(
  start?: EventTime | null,
  end?: EventTime | null,
  viewerZone = viewerTimeZone()
): string | undefined {
  if (start?.date && end?.date) {
    const lastDay = new Date(Date.parse(end.date) - DAY_MS);
    const firstDay = formatEventTime(start, viewerZone);
    if (lastDay.getTime() < Date.parse(start.date)) {
      return `${firstDay} – ${formatEventTime(end, viewerZone)} (exclusive end)`;
    }
    return `All day · ${firstDay}${lastDay.getTime() === Date.parse(start.date) ? "" : ` – ${utcDate.format(lastDay)}`}`;
  }
  if (start?.dateTime && end?.dateTime) {
    const from = timedParts(start.dateTime, start.timeZone, viewerZone);
    const to = timedParts(end.dateTime, end.timeZone, viewerZone);
    if (from && to && from.zone === to.zone) {
      const sameOffset = from.offset === to.offset;
      const startTime = sameOffset ? from.time : timeWithOffset(from);
      const endTime = sameOffset ? to.time : timeWithOffset(to);
      const right = from.day === to.day ? endTime : `${to.day} · ${endTime}`;
      return [from.day, `${startTime} – ${right}`, from.zone].filter(Boolean).join(" · ");
    }
    if (from && to) {
      const endpoint = (parts: TimedParts) =>
        [parts.day, timeWithOffset(parts), parts.zone].filter(Boolean).join(" · ");
      return `${endpoint(from)} – ${endpoint(to)}`;
    }
  }
  const from = start ? formatEventTime(start, viewerZone) : undefined;
  const to = end ? formatEventTime(end, viewerZone) : undefined;
  if (from && to) return `${from} – ${to}`;
  if (from) return start?.date ? `All day from ${from}` : `Starts ${from}`;
  if (to) return end?.date ? `Ends before ${to}` : `Ends ${to}`;
  return undefined;
}

export function formatDateBound(value: string): string {
  return dateTime.safeParse(value).success ? formatDateTime(value, undefined, viewerTimeZone()) : value;
}

export function formatRecurrence(line: string, start?: EventTime | null): string {
  if (!line.startsWith("RRULE:")) return line;
  try {
    const date = start?.date ?? start?.dateTime?.slice(0, 10);
    const rule = rrulestr(line, date ? { dtstart: new Date(`${date}T00:00:00Z`) } : undefined);
    if ("toText" in rule && rule.isFullyConvertibleToText()) {
      const text = rule.toText();
      return text ? `${text.charAt(0).toUpperCase()}${text.slice(1)}` : line;
    }
  } catch {
    // Keep the submitted line visible when the browser cannot explain it.
  }
  return line;
}
