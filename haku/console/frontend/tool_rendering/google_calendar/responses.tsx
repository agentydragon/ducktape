// Focused result rendering for the in-process `google_calendar` server. Every schema below is
// generated from FastMCP's advertised outputSchema; the backend's CalendarEvent projection is the
// shared create/get/list/instances wire contract.

import { Group, Stack } from "@mantine/core";
import type { z } from "zod";

import { Field } from "../../field.tsx";
import { ClockIcon, GoogleCalendarIcon, MapPinIcon, UsersIcon } from "../../icons.tsx";
import { ExternalLink } from "../../link.tsx";
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

// The event's own name is its identity, so — like every other card title — it leads unlabelled.
// When the event has a Calendar link, the title doubles as that link (Google Calendar's own
// icon marks it as external) instead of a separate "Open event in Google Calendar ↗" line below.
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

function CalendarEventResultView({ result, variant }: ResultPreviewProps<CalendarEvent>) {
  return <CalendarEventView event={result} variant={variant} />;
}

// `create_event`'s own argument preview already shows the event's when/recurrence (see
// requests.tsx's CreateCalendarEventPreview), so the result doesn't restate those — but the
// event's own name is still the identity, so it uses the same linked title every other event
// view does (EventTitle), not generic "Open event in Google Calendar ↗" text.
function CreateCalendarEventResultView({ result, variant }: ResultPreviewProps<CalendarEvent>) {
  return (
    <Stack gap={6}>
      <EventTitle event={result} />
      {variant === "detailed" && (
        <PreviewText size="xs" c="dimmed">
          event {result.event_id}
          {result.status ? ` · ${result.status}` : ""}
        </PreviewText>
      )}
    </Stack>
  );
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
  create_event: defineResultPreview(zCreateEventResult, CreateCalendarEventResultView),
  get_event: defineResultPreview(zGetEventResult, CalendarEventResultView),
  list_events: defineResultPreview(zListEventsResult, CalendarEventsPageResultView),
  list_event_instances: defineResultPreview(zListEventInstancesResult, CalendarEventsPageResultView),
} satisfies Record<string, ToolResultPreview>;
