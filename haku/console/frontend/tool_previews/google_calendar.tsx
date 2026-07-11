// Per-tool-type rendering for haku-console's in-process `google_calendar` MCP server (see
// haku/console/tools/google_calendar.py). Falls back to the generic raw-JSON view
// (approval_state.ts's argumentsJson) for anything that isn't shaped as expected. The zod
// schema below is generated from `create_calendar_event`'s Pydantic argument model
// (:schema_zod), so this file's shape checks can never drift from the backend's.

import { Stack, Text } from "@mantine/core";
import { format, formatDuration, intervalToDuration, parseISO } from "date-fns";
import type { ReactNode } from "react";
import type { z } from "zod";

import { zCalendarReminder, zCreateCalendarEventArgs, zEventDateTime } from "../api/schema.zod.ts";
import { Field } from "../field.tsx";
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
          {args.calendar_id && args.calendar_id !== "primary" && <Field label="Calendar">{args.calendar_id}</Field>}
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

/** Nice rendering for the `google_calendar` server's `create_calendar_event`; `null` when the
 * (server, tool, arguments) triple doesn't match, so the caller falls back to raw JSON. */
export function googleCalendarToolPreview(
  toolName: string,
  args: Record<string, unknown>,
  variant: PreviewVariant
): ReactNode | null {
  if (toolName === "create_calendar_event") {
    const parsed = zCreateCalendarEventArgs.safeParse(args);
    return parsed.success ? <CreateCalendarEventPreview args={parsed.data} variant={variant} /> : null;
  }
  return null;
}
