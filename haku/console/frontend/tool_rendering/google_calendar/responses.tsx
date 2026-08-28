// Focused result rendering for the in-process `google_calendar` server. Every schema below is
// generated from FastMCP's advertised outputSchema; the backend's CalendarEvent projection is the
// shared create/get/list/instances wire contract.

import { Group, Stack } from "@mantine/core";
import type { z } from "zod";

import { Field } from "../../field";
import { ClockIcon, GoogleCalendarIcon, MapPinIcon, UsersIcon } from "../../icons";
import { ExternalLink } from "../../link";
import { mcpToolResultSchema, type McpToolResultFor } from "../../mcp_tool_result_schema";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry";
import { COMPACT_ITEM_LIMIT, MoreLine, PreviewText, PreviewTitle } from "../vocabulary";
import { GOOGLE_CALENDAR_SERVER_ID } from "../server_ids";
import { formatEventDateTimeRange, RecurrenceField } from "./requests";

export const zCreateEventResult: z.ZodType<McpToolResultFor<typeof GOOGLE_CALENDAR_SERVER_ID, "create_event">> =
  mcpToolResultSchema(GOOGLE_CALENDAR_SERVER_ID, "create_event");
const zGetEventResult: z.ZodType<McpToolResultFor<typeof GOOGLE_CALENDAR_SERVER_ID, "get_event">> = mcpToolResultSchema(
  GOOGLE_CALENDAR_SERVER_ID,
  "get_event"
);
const zListEventsResult: z.ZodType<McpToolResultFor<typeof GOOGLE_CALENDAR_SERVER_ID, "list_events">> =
  mcpToolResultSchema(GOOGLE_CALENDAR_SERVER_ID, "list_events");
const zListEventInstancesResult: z.ZodType<McpToolResultFor<typeof GOOGLE_CALENDAR_SERVER_ID, "list_event_instances">> =
  mcpToolResultSchema(GOOGLE_CALENDAR_SERVER_ID, "list_event_instances");

export type CalendarEvent = z.infer<typeof zCreateEventResult>;
type CalendarEventsPage = z.infer<typeof zListEventsResult>;

// The event's own name is its identity, so it leads unlabelled. When the event has a Calendar link
// the title doubles as that link, marked external by Google Calendar's own icon, instead of a
// separate "Open event in Google Calendar ↗" line below.
function EventTitle({ event }: { event: CalendarEvent }) {
  const title = event.summary ?? event.event_id;
  if (!event.html_link) return <PreviewTitle>{title}</PreviewTitle>;
  return (
    <ExternalLink href={event.html_link} size="sm" fw={600}>
      <Group gap={4} wrap="nowrap" align="center">
        <GoogleCalendarIcon size={15} />
        <span>{title}</span>
      </Group>
    </ExternalLink>
  );
}

function CalendarEventView({ event, variant }: { event: CalendarEvent; variant: "compact" | "detailed" }) {
  const when = event.start && event.end ? formatEventDateTimeRange(event.start, event.end) : null;
  const recurrence = event.recurrence ?? [];
  const attendees = event.attendees ?? [];
  return (
    <Stack gap={6}>
      <EventTitle event={event} />
      {when && (
        <Field icon={<ClockIcon size={15} />} label="When">
          <span title={when.title}>{when.text}</span>
        </Field>
      )}
      {recurrence.length > 0 && <RecurrenceField recurrence={recurrence} variant={variant} />}
      {variant === "detailed" && (
        <>
          {event.location && (
            <Field icon={<MapPinIcon size={15} />} label="Location">
              {event.location}
            </Field>
          )}
          {event.description && <PreviewText c="dimmed">{event.description}</PreviewText>}
          {attendees.length > 0 && (
            <Field icon={<UsersIcon size={15} />} label="Attendees">
              {attendees.map((attendee) => attendee.display_name ?? attendee.email).join(", ")}
            </Field>
          )}
          {event.recurring_event_id && (
            <Field label="Series" mono>
              {event.recurring_event_id}
            </Field>
          )}
          <PreviewText size="xs" c="dimmed">
            event {event.event_id}
            {event.status ? ` · ${event.status}` : ""}
          </PreviewText>
        </>
      )}
    </Stack>
  );
}

// Exported for google_calendar/calls.tsx's combined create_event widget, rendered once the tool has
// executed — the same full event view get_event/list_events use, and the only place the event's
// when/recurrence/location shows after the call finishes.
export function CalendarEventResultView({ result, variant }: ResultPreviewProps<CalendarEvent>): JSX.Element {
  return <CalendarEventView event={result} variant={variant} />;
}

function CalendarEventsPageResultView({ result, variant }: ResultPreviewProps<CalendarEventsPage>) {
  const events = result.events ?? [];
  const shown = variant === "compact" ? events.slice(0, COMPACT_ITEM_LIMIT) : events;
  return (
    <Stack gap="xs">
      {shown.length > 0 ? (
        shown.map((event) => <CalendarEventView key={event.event_id} event={event} variant={variant} />)
      ) : (
        <PreviewText c="dimmed">No events</PreviewText>
      )}
      {variant === "compact" && <MoreLine count={events.length - shown.length} />}
      {result.next_page_token && variant === "detailed" && (
        <PreviewText size="xs" c="dimmed">
          More events available
        </PreviewText>
      )}
    </Stack>
  );
}

/** Per-tool result widgets for the `google_calendar` server. `create_event` has no entry here —
 * its pending/finished states are one combined widget (calls.tsx), not a separate result-only
 * one. */
export const googleCalendarResultPreviews: {
  get_event: ToolResultPreview<typeof zGetEventResult>;
  list_events: ToolResultPreview<typeof zListEventsResult>;
  list_event_instances: ToolResultPreview<typeof zListEventInstancesResult>;
} = {
  get_event: defineResultPreview(zGetEventResult, CalendarEventResultView),
  list_events: defineResultPreview(zListEventsResult, CalendarEventsPageResultView),
  list_event_instances: defineResultPreview(zListEventInstancesResult, CalendarEventsPageResultView),
} satisfies Record<string, ToolResultPreview>;
