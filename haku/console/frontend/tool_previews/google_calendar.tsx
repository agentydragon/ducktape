// Per-tool-type rendering for haku-console's in-process `google_calendar` MCP server (see
// haku/console/tools/google_calendar.py). Falls back to the generic raw-JSON view
// (approval_state.ts's argumentsJson) for anything that isn't shaped as expected. The zod
// schema below is generated from `create_calendar_event`'s Pydantic argument model
// (:schema_zod), so this file's shape checks can never drift from the backend's.

import { Anchor, Loader, Stack, Text } from "@mantine/core";
import { format, formatDuration, intervalToDuration, parseISO } from "date-fns";
import { useEffect, useState } from "react";
import type { z } from "zod";

import { zCalendarReminder, zCreateCalendarEventArgs, zEventDateTime } from "../api/schema.zod.ts";
import { fetchCalendarSummary, type CalendarSummary } from "../calendar_client.ts";
import { Field } from "../field.tsx";
import { definePreview, type ToolPreview } from "./entry.tsx";
import type { PreviewVariant } from "./variant.tsx";

export const GOOGLE_CALENDAR_SERVER_ID = "google_calendar";

type EventDateTime = z.infer<typeof zEventDateTime>;
type CalendarReminder = z.infer<typeof zCalendarReminder>;
type CreateCalendarEventArgs = z.infer<typeof zCreateCalendarEventArgs>;

function formatEventDateTime(value: EventDateTime): string {
  // All-day dates carry no zone ambiguity, so render them nicely. Timed events keep their
  // RFC3339 string verbatim (it already encodes the exact instant + offset) plus the IANA
  // zone the caller gave — reformatting to the viewer's local zone would misstate the
  // operator's intent, and zone-correct display would need date-fns-tz, which we don't pull in.
  if (value.date) return format(parseISO(value.date), "EEE, d MMM yyyy");
  if (value.date_time) return value.time_zone ? `${value.date_time} (${value.time_zone})` : value.date_time;
  return "(unset)";
}

function formatReminder(reminder: CalendarReminder): string {
  const timing =
    reminder.minutes_before_start === 0
      ? "at event start"
      : `${formatDuration(intervalToDuration({ start: 0, end: reminder.minutes_before_start * 60_000 }))} before`;
  return `${reminder.method === "popup" ? "Popup" : "Email"}, ${timing}`;
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
    <Field label="Calendar">
      {summary ? (
        <Anchor href={summary.html_link} target="_blank" rel="noreferrer">
          {summary.summary}
        </Anchor>
      ) : failed ? (
        // Name lookup failed (e.g. deleted calendar, wrong account) — fall back to the raw id.
        <Text span>{calendarId}</Text>
      ) : (
        <Loader size="xs" />
      )}
    </Field>
  );
}

function CreateCalendarEventPreview({ args, variant }: { args: CreateCalendarEventArgs; variant: PreviewVariant }) {
  // Common trunk: the wrapper + event summary; compact stops at the start time, detailed
  // expands the full when/where/who.
  return (
    <Stack gap="xs">
      <Field label="Event">{args.summary}</Field>
      {variant === "compact" ? (
        <Field label="When">{formatEventDateTime(args.start)}</Field>
      ) : (
        <>
          <div className="haku-shell-field-grid">
            <Field label="Start">{formatEventDateTime(args.start)}</Field>
            <Field label="End">{formatEventDateTime(args.end)}</Field>
          </div>
          {args.location && <Field label="Location">{args.location}</Field>}
          {args.description && <Field label="Description">{args.description}</Field>}
          {args.calendar_id && args.calendar_id !== "primary" && <CalendarField calendarId={args.calendar_id} />}
          {args.reminders && args.reminders.length > 0 && (
            <Field label="Reminders">
              <Stack gap={2}>
                {args.reminders.map((reminder, i) => (
                  <Text size="sm" key={i}>
                    {formatReminder(reminder)}
                  </Text>
                ))}
              </Stack>
            </Field>
          )}
          {args.attendees && args.attendees.length > 0 && <Field label="Attendees">{args.attendees.join(", ")}</Field>}
        </>
      )}
    </Stack>
  );
}

/** Per-tool preview widgets for the `google_calendar` server. */
export const googleCalendarPreviews = {
  create_calendar_event: definePreview(zCreateCalendarEventArgs, (args, variant) => (
    <CreateCalendarEventPreview args={args} variant={variant} />
  )),
} satisfies Record<string, ToolPreview>;
