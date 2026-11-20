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

function timedParts(value: string, zone?: string): { day: string; time: string; zone?: string } | undefined {
  const offset = value.match(/(Z|[+-]\d\d:\d\d)$/)?.[0];
  const zoned = offset && zone;
  const instant = new Date(zoned ? value : `${offset ? value.slice(0, -offset.length) : value}Z`);
  try {
    const timeZone = zoned ? zone : "UTC";
    return {
      day: new Intl.DateTimeFormat("en-US", { ...SHORT_DATE, timeZone }).format(instant),
      time: new Intl.DateTimeFormat("en-US", {
        ...SHORT_TIME,
        timeZone,
        ...(zoned ? { timeZoneName: "shortOffset" } : {}),
      }).format(instant),
      zone: zone ?? (offset === "Z" ? "UTC" : offset),
    };
  } catch {
    return undefined;
  }
}

function formatDateTime(value: string, zone?: string): string {
  const parts = timedParts(value, zone);
  if (parts) return [parts.day, parts.time, parts.zone].filter(Boolean).join(" · ");
  return zone ? `${value} · ${zone}` : value;
}

export function formatEventTime(value: EventTime): string {
  if (value.date !== undefined) return utcDate.format(new Date(value.date));
  return value.dateTime === undefined ? "" : formatDateTime(value.dateTime, value.timeZone);
}

export function formatEventRange(start?: EventTime | null, end?: EventTime | null): string | undefined {
  if (start?.date && end?.date) {
    const lastDay = new Date(Date.parse(end.date) - DAY_MS);
    const firstDay = formatEventTime(start);
    if (lastDay.getTime() < Date.parse(start.date)) {
      return `${firstDay} – ${formatEventTime(end)} (exclusive end)`;
    }
    return `All day · ${firstDay}${lastDay.getTime() === Date.parse(start.date) ? "" : ` – ${utcDate.format(lastDay)}`}`;
  }
  if (start?.dateTime && end?.dateTime) {
    const from = timedParts(start.dateTime, start.timeZone);
    const to = timedParts(end.dateTime, end.timeZone);
    if (from && to && from.day === to.day && from.zone === to.zone) {
      return [from.day, `${from.time} – ${to.time}`, from.zone].filter(Boolean).join(" · ");
    }
  }
  const from = start ? formatEventTime(start) : undefined;
  const to = end ? formatEventTime(end) : undefined;
  if (from && to) return `${from} – ${to}`;
  if (from) return start?.date ? `All day from ${from}` : `Starts ${from}`;
  if (to) return end?.date ? `Ends before ${to}` : `Ends ${to}`;
  return undefined;
}

export function formatDateBound(value: string): string {
  return dateTime.safeParse(value).success ? formatDateTime(value) : value;
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
