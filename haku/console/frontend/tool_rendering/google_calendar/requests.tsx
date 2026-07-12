// Per-tool-type rendering for haku-console's in-process `google_calendar` MCP server (see
// haku/console/tools/google_calendar.py). Falls back to the generic raw-JSON view
// (approval_state.ts's argumentsJson) for anything that isn't shaped as expected. The zod
// schema below is built from the FastMCP input schema advertised by tools/list. Execution-only
// Pydantic cross-field validators may be stricter than that structural schema.

import { Anchor, Loader, Stack, Text } from "@mantine/core";
import { useEffect, useState } from "react";
import type { z } from "zod";

import { fetchCalendarSummary, type CalendarSummary } from "../../calendar_client.ts";
import { Field } from "../../field.tsx";
import { BellIcon, CalendarIcon, ClockIcon, MapPinIcon, UsersIcon } from "../../icons.tsx";
import { mcpToolSchema } from "../../mcp_tool_schema.ts";
import { definePreview, type ToolPreview } from "../entry.tsx";
import type { PreviewProps } from "../variant.tsx";

export const GOOGLE_CALENDAR_SERVER_ID = "google_calendar";

const zCreateCalendarEventArgs = mcpToolSchema(GOOGLE_CALENDAR_SERVER_ID, "create_calendar_event");

type CreateCalendarEventArgs = z.infer<typeof zCreateCalendarEventArgs>;
type EventDateTime = CreateCalendarEventArgs["start"];
type CalendarReminder = NonNullable<CreateCalendarEventArgs["reminders"]>[number];

function formatEventDateTime(value: EventDateTime): string {
  if (value.date) return value.date;
  if (value.date_time) return value.time_zone ? `${value.date_time} (${value.time_zone})` : value.date_time;
  return "(unset)";
}

// A Google Calendar `date_time` ("2026-07-12T18:00:00") is a wall-clock time in the event's
// own `time_zone` (the string carries no offset), so we read the date and time straight off
// the string rather than parsing to an instant — no accidental shift into the viewer's zone.
type ParsedEventDate =
  | { allDay: true; y: number; mo: number; d: number }
  | { allDay: false; y: number; mo: number; d: number; time: string };

function parseEventDate(value: EventDateTime): ParsedEventDate | null {
  const source = value.date ?? value.date_time;
  if (!source) return null;
  const [datePart, timePart] = source.split("T");
  const [y, mo, d] = datePart.split("-").map(Number);
  if (!y || !mo || !d) return null;
  if (value.date) return { allDay: true, y, mo, d };
  if (!timePart) return null;
  return { allDay: false, y, mo, d, time: timePart.slice(0, 5) };
}

// Formatters over a UTC-normalized calendar date, so the weekday/month names are locale-aware
// but the date itself is read as the event's own (already-local) wall-clock, never converted.
const _WEEKDAY_LONG = new Intl.DateTimeFormat(undefined, { weekday: "long", timeZone: "UTC" });
const _WEEKDAY_SHORT = new Intl.DateTimeFormat(undefined, { weekday: "short", timeZone: "UTC" });
const _MONTH_DAY = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
const _FULL_DATE = new Intl.DateTimeFormat(undefined, {
  weekday: "long",
  year: "numeric",
  month: "long",
  day: "numeric",
  timeZone: "UTC",
});

const asUtc = (p: ParsedEventDate): Date => new Date(Date.UTC(p.y, p.mo - 1, p.d));
const sameDay = (a: ParsedEventDate, b: ParsedEventDate): boolean => a.y === b.y && a.mo === b.mo && a.d === b.d;

function zoneSuffix(timeZone: string | null | undefined, at: Date): { short: string; full: string } {
  if (!timeZone) return { short: "", full: "" };
  const viewer = new Intl.DateTimeFormat().resolvedOptions().timeZone;
  const full = ` (${timeZone})`;
  if (timeZone === viewer) return { short: "", full }; // same zone as the reader → not worth showing
  const part = new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "short" })
    .formatToParts(at)
    .find((p) => p.type === "timeZoneName");
  return { short: ` ${part ? part.value : timeZone}`, full };
}

/** Collapse an event's start–end into one line: same-day timed → "Thursday 18:00–19:00"; a
 * span of days keeps both ends; all-day reads "… · all day" (Google's exclusive end shown
 * inclusively). Zone is appended only when it isn't the viewer's. Returns the concise `text`
 * plus a full-precision `title` (with year + IANA zone) for the element's tooltip. */
export function formatEventDateTimeRange(start: EventDateTime, end: EventDateTime): { text: string; title: string } {
  const s = parseEventDate(start);
  const e = parseEventDate(end);
  if (!s || !e || s.allDay !== e.allDay) {
    const raw = `${formatEventDateTime(start)} – ${formatEventDateTime(end)}`;
    return { text: raw, title: raw };
  }
  if (s.allDay && e.allDay) {
    // Google's all-day end date is exclusive; show it inclusively (one day back).
    const endIncl = new Date(Date.UTC(e.y, e.mo - 1, e.d - 1));
    const startUtc = asUtc(s);
    if (endIncl.getTime() <= startUtc.getTime()) {
      return {
        text: `${_WEEKDAY_LONG.format(startUtc)}, ${_MONTH_DAY.format(startUtc)} · all day`,
        title: `${_FULL_DATE.format(startUtc)} — all day`,
      };
    }
    return {
      text: `${_MONTH_DAY.format(startUtc)} – ${_MONTH_DAY.format(endIncl)} · all day`,
      title: `${_FULL_DATE.format(startUtc)} – ${_FULL_DATE.format(endIncl)} — all day`,
    };
  }
  if (!s.allDay && !e.allDay) {
    const zone = zoneSuffix(start.time_zone, new Date(`${asUtc(s).toISOString().slice(0, 11)}${s.time}:00Z`));
    if (sameDay(s, e)) {
      return {
        text: `${_WEEKDAY_LONG.format(asUtc(s))} ${s.time}–${e.time}${zone.short}`,
        title: `${_FULL_DATE.format(asUtc(s))} · ${s.time}–${e.time}${zone.full}`,
      };
    }
    return {
      text: `${_WEEKDAY_SHORT.format(asUtc(s))} ${s.time} – ${_WEEKDAY_SHORT.format(asUtc(e))} ${e.time}${zone.short}`,
      title: `${_FULL_DATE.format(asUtc(s))} ${s.time} – ${_FULL_DATE.format(asUtc(e))} ${e.time}${zone.full}`,
    };
  }
  const raw = `${formatEventDateTime(start)} – ${formatEventDateTime(end)}`;
  return { text: raw, title: raw };
}

// Compact reminder offset — just the lead time, no method: "30 min before", "1h 30m before",
// "1 day before", "1 week before", "at start". Largest whole unit down; "min" alone, "h"/"m"
// when combined.
function formatReminder(reminder: CalendarReminder): string {
  const m = reminder.minutes_before_start;
  if (m === 0) return "at start";
  if (m % 10080 === 0) return `${m / 10080} week${m === 10080 ? "" : "s"} before`;
  if (m % 1440 === 0) return `${m / 1440} day${m === 1440 ? "" : "s"} before`;
  if (m < 60) return `${m} min before`;
  const h = Math.floor(m / 60);
  const mins = m % 60;
  return `${mins ? `${h}h ${mins}m` : `${h}h`} before`;
}

// A non-primary calendar's id is opaque; resolve its display name (linked into Google Calendar)
// via the console read endpoint. On failure the raw id still renders — the operator sees the
// target either way. Only shown for non-primary calendars (see the caller).
function CalendarField({ calendarId }: { calendarId: string }) {
  const [summary, setSummary] = useState<CalendarSummary | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setSummary(null);
    setFailed(false);
    fetchCalendarSummary(calendarId)
      .then((result) => {
        if (alive) setSummary(result);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [calendarId]);

  return (
    <Field icon={<CalendarIcon size={15} />} label="Calendar">
      {summary ? (
        <Anchor href={summary.html_link} target="_blank" rel="noreferrer" size="sm">
          {summary.summary}
        </Anchor>
      ) : failed ? (
        // Name lookup failed (e.g. deleted calendar, wrong account) — fall back to the raw id.
        <Text size="sm" span>
          {calendarId}
        </Text>
      ) : (
        <Loader size="xs" />
      )}
    </Field>
  );
}

function CreateCalendarEventPreview({ args, variant }: PreviewProps<CreateCalendarEventArgs>) {
  // The summary is the event's own name, so it leads as a heading (no label); the rest use the
  // inline icon fields (🕐 time, 📍 location, …) — denser than a stacked uppercase label per row.
  const when = formatEventDateTimeRange(args.start, args.end);
  return (
    <Stack gap={6}>
      <Text size="sm" fw={600}>
        {args.summary}
      </Text>
      <Field icon={<ClockIcon size={15} />} label="When">
        <span title={when.title}>{when.text}</span>
      </Field>
      {variant === "detailed" && (
        <>
          {args.location && (
            <Field icon={<MapPinIcon size={15} />} label="Location">
              {args.location}
            </Field>
          )}
          {args.description && (
            <Text size="sm" c="dimmed">
              {args.description}
            </Text>
          )}
          {args.calendar_id && args.calendar_id !== "primary" && <CalendarField calendarId={args.calendar_id} />}
          {args.reminders && args.reminders.length > 0 && (
            <Field icon={<BellIcon size={15} />} label="Reminders">
              {args.reminders.map(formatReminder).join(" · ")}
            </Field>
          )}
          {args.attendees && args.attendees.length > 0 && (
            <Field icon={<UsersIcon size={15} />} label="Attendees">
              {args.attendees.join(", ")}
            </Field>
          )}
        </>
      )}
    </Stack>
  );
}

/** Per-tool preview widgets for the `google_calendar` server. */
export const googleCalendarPreviews = {
  create_calendar_event: definePreview(zCreateCalendarEventArgs, CreateCalendarEventPreview, () => ({
    text: "Google Calendar: Create event",
  })),
} satisfies Record<string, ToolPreview>;
