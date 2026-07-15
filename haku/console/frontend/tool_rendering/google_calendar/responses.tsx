// Focused result rendering for the in-process `google_calendar` server. Every schema below is
// generated from FastMCP's advertised outputSchema; the backend's CalendarEvent projection is the
// shared create/get/list/instances wire contract.

import { Anchor, Stack } from "@mantine/core";
import type { z } from "zod";

import { Field } from "../../field.tsx";
import { ClockIcon, MapPinIcon, UsersIcon } from "../../icons.tsx";
import { mcpToolResultSchema } from "../../mcp_tool_result_schema.ts";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry.tsx";
import { COMPACT_ITEM_LIMIT, MoreLine, PreviewText, PreviewTitle } from "../vocabulary.tsx";
import { formatEventDateTimeRange, GOOGLE_CALENDAR_SERVER_ID, RecurrenceField } from "./requests.tsx";

const zCreateEventResult = mcpToolResultSchema(GOOGLE_CALENDAR_SERVER_ID, "create_event");
const zGetEventResult = mcpToolResultSchema(GOOGLE_CALENDAR_SERVER_ID, "get_event");
const zListEventsResult = mcpToolResultSchema(GOOGLE_CALENDAR_SERVER_ID, "list_events");
const zListEventInstancesResult = mcpToolResultSchema(GOOGLE_CALENDAR_SERVER_ID, "list_event_instances");

type CalendarEvent = z.infer<typeof zCreateEventResult>;
type CalendarEventsPage = z.infer<typeof zListEventsResult>;

function CalendarEventView({ event, variant }: { event: CalendarEvent; variant: "compact" | "detailed" }) {
  const when = event.start && event.end ? formatEventDateTimeRange(event.start, event.end) : null;
  const recurrence = event.recurrence ?? [];
  const attendees = event.attendees ?? [];
  return (
    <Stack gap={6}>
      <PreviewTitle>{event.summary ?? event.event_id}</PreviewTitle>
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
        </>
      )}
      {event.html_link && (
        <Anchor href={event.html_link} target="_blank" rel="noreferrer" size="sm">
          Open event in Google Calendar ↗
        </Anchor>
      )}
      {variant === "detailed" && (
        <PreviewText size="xs" c="dimmed">
          event {event.event_id}
          {event.status ? ` · ${event.status}` : ""}
        </PreviewText>
      )}
    </Stack>
  );
}

function CalendarEventResultView({ result, variant }: ResultPreviewProps<CalendarEvent>) {
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

/** Per-tool result widgets for the `google_calendar` server. */
export const googleCalendarResultPreviews = {
  create_event: defineResultPreview(zCreateEventResult, CalendarEventResultView),
  get_event: defineResultPreview(zGetEventResult, CalendarEventResultView),
  list_events: defineResultPreview(zListEventsResult, CalendarEventsPageResultView),
  list_event_instances: defineResultPreview(zListEventInstancesResult, CalendarEventsPageResultView),
} satisfies Record<string, ToolResultPreview>;
