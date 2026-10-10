import { Anchor, Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { Chip } from "./chips";
import { definePreview, type ArgumentsPreview } from "./entry";
import { defineResultPreview, type ResultPreview } from "./result_entry";

const eventDateTime = z
  .strictObject({
    date: z.string().nullable().optional(),
    date_time: z.string().nullable().optional(),
    time_zone: z.string().nullable().optional(),
  })
  .refine((value) => Boolean(value.date) !== Boolean(value.date_time));

function dateTimeText(value: z.infer<typeof eventDateTime>): string {
  if (value.date) return `${value.date} · all day`;
  if (!value.date_time) return "(not set)";
  return value.time_zone ? `${value.date_time} (${value.time_zone})` : value.date_time;
}

function eventRange(start: z.infer<typeof eventDateTime>, end: z.infer<typeof eventDateTime>): string {
  return `${dateTimeText(start)} – ${dateTimeText(end)}`;
}

const reminder = z.strictObject({
  method: z.string().optional(),
  minutes_before_start: z.int().nonnegative(),
});
const createEventArguments = z.strictObject({
  summary: z.string(),
  start: eventDateTime,
  end: eventDateTime,
  description: z.string().nullable().optional(),
  location: z.string().nullable().optional(),
  calendar_id: z.string().optional(),
  reminders: z.array(reminder).nullable().optional(),
  attendees: z.array(z.string()).nullable().optional(),
  recurrence: z.array(z.string()).nullable().optional(),
});

type CreateEventArguments = z.infer<typeof createEventArguments>;

function CreateEventLabel({ args }: { args: CreateEventArguments }): JSX.Element {
  return <Text fw={600}>Create calendar event{args.summary ? `: ${args.summary}` : ""}</Text>;
}

function CreateEventBody({ args, includeDescription }: { args: CreateEventArguments; includeDescription: boolean }) {
  return (
    <Stack gap="xs">
      {!args.summary && <Chip label="Event title" value="(empty)" />}
      <Chip label="When" value={eventRange(args.start, args.end)} />
      {args.location != null && <Chip label="Location" value={args.location || "(empty)"} />}
      {args.recurrence && args.recurrence.length > 0 && <Chip label="Repeats" value={args.recurrence.join(" · ")} />}
      {includeDescription && args.description != null && (
        <Chip label="Description" value={args.description || "(empty)"} />
      )}
      {includeDescription && args.calendar_id && args.calendar_id !== "primary" && (
        <Chip label="Calendar ID" value={args.calendar_id} />
      )}
      {includeDescription && args.reminders && args.reminders.length > 0 && (
        <Chip
          label="Reminders"
          value={args.reminders
            .map(({ method, minutes_before_start }) => `${method ?? "popup"} ${minutes_before_start} min before`)
            .join(" · ")}
        />
      )}
      {includeDescription && args.attendees && args.attendees.length > 0 && (
        <Chip label="Attendees" value={args.attendees.join(", ")} />
      )}
    </Stack>
  );
}

function CreateEventCollapsed({ args }: { args: CreateEventArguments }): JSX.Element {
  return <Text size="sm">{eventRange(args.start, args.end)}</Text>;
}

function CreateEventOpened({ args }: { args: CreateEventArguments }): JSX.Element {
  return <CreateEventBody args={args} includeDescription={false} />;
}

function CreateEventDetails({ args }: { args: CreateEventArguments }): JSX.Element {
  return <CreateEventBody args={args} includeDescription />;
}

export const createEventLabel: ArgumentsPreview = definePreview(createEventArguments, CreateEventLabel);
export const createEventCollapsed: ArgumentsPreview = definePreview(createEventArguments, CreateEventCollapsed);
export const createEventOpened: ArgumentsPreview = definePreview(createEventArguments, CreateEventOpened);
export const createEventDetails: ArgumentsPreview = definePreview(createEventArguments, CreateEventDetails);

const updateEventArguments = z.strictObject({
  event_id: z.string(),
  calendar_id: z.string().optional(),
  summary: z.string().nullable().optional(),
  start: eventDateTime.nullable().optional(),
  end: eventDateTime.nullable().optional(),
  description: z.string().nullable().optional(),
  location: z.string().nullable().optional(),
  reminders: z.array(reminder).nullable().optional(),
  attendees: z.array(z.string()).nullable().optional(),
  recurrence: z.array(z.string()).nullable().optional(),
});

type UpdateEventArguments = z.infer<typeof updateEventArguments>;

function UpdateEventLabel({ args }: { args: UpdateEventArguments }): JSX.Element {
  return (
    <Text fw={600}>
      {args.summary != null && args.summary !== ""
        ? `Update calendar event: ${args.summary}`
        : `Update calendar event ${args.event_id}`}
    </Text>
  );
}

function UpdateEventBody({ args, detailed }: { args: UpdateEventArguments; detailed: boolean }) {
  const hasRange = args.start !== undefined && args.start !== null && args.end !== undefined && args.end !== null;
  const hasStart = args.start !== undefined && args.start !== null;
  const hasEnd = args.end !== undefined && args.end !== null;
  const hasVisibleDetail =
    hasStart ||
    hasEnd ||
    args.location != null ||
    args.recurrence != null ||
    (!detailed && args.summary === "") ||
    (detailed &&
      (args.summary != null ||
        args.calendar_id != null ||
        args.description != null ||
        args.reminders != null ||
        args.attendees != null));
  return (
    <Stack gap="xs">
      {hasRange && <Chip label="New time" value={eventRange(args.start!, args.end!)} />}
      {!hasRange && hasStart && <Chip label="New start" value={dateTimeText(args.start!)} />}
      {!hasRange && hasEnd && <Chip label="New end" value={dateTimeText(args.end!)} />}
      {args.location != null && <Chip label="New location" value={args.location || "(empty)"} />}
      {args.recurrence != null && (
        <Chip label="New recurrence" value={args.recurrence.length > 0 ? args.recurrence.join(" · ") : "none"} />
      )}
      {!detailed && args.summary === "" && <Chip label="New title" value="(empty)" />}
      {detailed && args.summary != null && args.summary !== "" && <Chip label="Event ID" value={args.event_id} />}
      {detailed && args.calendar_id && args.calendar_id !== "primary" && (
        <Chip label="Calendar ID" value={args.calendar_id} />
      )}
      {detailed && args.summary === "" && <Chip label="New title" value="(empty)" />}
      {detailed && args.description != null && <Chip label="New description" value={args.description || "(empty)"} />}
      {detailed && args.reminders != null && (
        <Chip
          label="New reminders"
          value={
            args.reminders.length > 0
              ? args.reminders
                  .map(({ method, minutes_before_start }) => `${method ?? "popup"} ${minutes_before_start} min before`)
                  .join(" · ")
              : "none"
          }
        />
      )}
      {detailed && args.attendees != null && (
        <Chip label="New attendees" value={args.attendees.length > 0 ? args.attendees.join(", ") : "none"} />
      )}
      {!hasVisibleDetail && (
        <Text size="sm" c="dimmed">
          {args.summary ? "Only the event title is changing." : "No event fields are being changed."}
        </Text>
      )}
    </Stack>
  );
}

function UpdateEventCollapsed({ args }: { args: UpdateEventArguments }): JSX.Element {
  const hasStart = args.start !== undefined && args.start !== null;
  const hasEnd = args.end !== undefined && args.end !== null;
  return (
    <Text size="sm">
      {hasStart && hasEnd
        ? eventRange(args.start!, args.end!)
        : hasStart
          ? `New start: ${dateTimeText(args.start!)}`
          : hasEnd
            ? `New end: ${dateTimeText(args.end!)}`
            : "Update requested fields"}
    </Text>
  );
}

function UpdateEventOpened({ args }: { args: UpdateEventArguments }): JSX.Element {
  return <UpdateEventBody args={args} detailed={false} />;
}

function UpdateEventDetails({ args }: { args: UpdateEventArguments }): JSX.Element {
  return <UpdateEventBody args={args} detailed />;
}

export const updateEventLabel: ArgumentsPreview = definePreview(updateEventArguments, UpdateEventLabel);
export const updateEventCollapsed: ArgumentsPreview = definePreview(updateEventArguments, UpdateEventCollapsed);
export const updateEventOpened: ArgumentsPreview = definePreview(updateEventArguments, UpdateEventOpened);
export const updateEventDetails: ArgumentsPreview = definePreview(updateEventArguments, UpdateEventDetails);

const eventTargetArguments = z.strictObject({ event_id: z.string(), calendar_id: z.string().optional() });

function EventTarget({ args }: { args: z.infer<typeof eventTargetArguments> }): JSX.Element {
  return (
    <Stack gap="xs">
      <Chip label="Event ID" value={args.event_id} />
      {args.calendar_id && args.calendar_id !== "primary" && <Chip label="Calendar ID" value={args.calendar_id} />}
    </Stack>
  );
}

export const getEventArguments: ArgumentsPreview = definePreview(eventTargetArguments, EventTarget);
export const deleteEventArguments: ArgumentsPreview = definePreview(eventTargetArguments, EventTarget);

const listEventsSchema = z.strictObject({
  calendar_id: z.string().optional(),
  time_min: z.string().nullable().optional(),
  time_max: z.string().nullable().optional(),
  query: z.string().nullable().optional(),
  expand_recurring: z.boolean().nullable().optional(),
  max_results: z.int().positive().nullable().optional(),
  page_token: z.string().nullable().optional(),
});

function ListEvents({ args }: { args: z.infer<typeof listEventsSchema> }): JSX.Element {
  return (
    <Stack gap="xs">
      <Chip label="Calendar ID" value={args.calendar_id ?? "primary"} />
      {(args.time_min || args.time_max) && (
        <Chip label="Time window" value={`${args.time_min ?? "any time"} – ${args.time_max ?? "any time"}`} />
      )}
      {args.query && <Chip label="Search" value={args.query} />}
      {args.expand_recurring != null && <Chip label="Expand recurring events" value={String(args.expand_recurring)} />}
      {args.max_results != null && <Chip label="Maximum results" value={args.max_results} />}
      {args.page_token && <Chip label="Page token" value={args.page_token} />}
    </Stack>
  );
}

export const listEventsArguments: ArgumentsPreview = definePreview(listEventsSchema, ListEvents);

const listInstancesArguments = z.strictObject({
  recurring_event_id: z.string(),
  calendar_id: z.string().optional(),
  time_min: z.string().nullable().optional(),
  time_max: z.string().nullable().optional(),
  max_results: z.int().positive().nullable().optional(),
  page_token: z.string().nullable().optional(),
});

function ListInstances({ args }: { args: z.infer<typeof listInstancesArguments> }): JSX.Element {
  return (
    <Stack gap="xs">
      <Chip label="Recurring event ID" value={args.recurring_event_id} />
      <Chip label="Calendar ID" value={args.calendar_id ?? "primary"} />
      {(args.time_min || args.time_max) && (
        <Chip label="Time window" value={`${args.time_min ?? "any time"} – ${args.time_max ?? "any time"}`} />
      )}
      {args.max_results != null && <Chip label="Maximum results" value={args.max_results} />}
      {args.page_token && <Chip label="Page token" value={args.page_token} />}
    </Stack>
  );
}

export const listEventInstancesArguments: ArgumentsPreview = definePreview(listInstancesArguments, ListInstances);

const resultDateTime = z
  .object({
    date: z.string().nullable().optional(),
    date_time: z.string().nullable().optional(),
    time_zone: z.string().nullable().optional(),
  })
  .passthrough();
const calendarEvent = z
  .object({
    event_id: z.string(),
    status: z.string().nullable().optional(),
    summary: z.string().nullable().optional(),
    description: z.string().nullable().optional(),
    location: z.string().nullable().optional(),
    start: resultDateTime.nullable().optional(),
    end: resultDateTime.nullable().optional(),
    recurrence: z.array(z.string()).optional(),
    recurring_event_id: z.string().nullable().optional(),
    attendees: z
      .array(
        z
          .object({ email: z.string().nullable().optional(), display_name: z.string().nullable().optional() })
          .passthrough()
      )
      .optional(),
    html_link: z.string().nullable().optional(),
  })
  .passthrough();

function safeCalendarUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && ["calendar.google.com", "www.google.com"].includes(url.hostname) ? value : null;
  } catch {
    return null;
  }
}

function CalendarEvent({ event }: { event: z.infer<typeof calendarEvent> }): JSX.Element {
  const title = event.summary || event.event_id;
  const href = safeCalendarUrl(event.html_link);
  return (
    <Stack gap={4}>
      {href ? (
        <Anchor href={href} target="_blank" rel="noreferrer" fw={600}>
          {title}
        </Anchor>
      ) : (
        <Text fw={600}>{title}</Text>
      )}
      {event.start && event.end && <Chip label="When" value={eventRange(event.start, event.end)} />}
      {event.location && <Chip label="Location" value={event.location} />}
      {event.recurrence && event.recurrence.length > 0 && <Chip label="Repeats" value={event.recurrence.join(" · ")} />}
      {event.attendees && event.attendees.length > 0 && (
        <Chip
          label="Attendees"
          value={event.attendees.map((attendee) => attendee.display_name ?? attendee.email ?? "(unknown)").join(", ")}
        />
      )}
      {event.description && (
        <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {event.description}
        </Text>
      )}
      <Text size="xs" c="dimmed">
        event {event.event_id}
        {event.status ? ` · ${event.status}` : ""}
      </Text>
    </Stack>
  );
}

const oneEventResult = calendarEvent;

function EventResult({ result }: { result: z.infer<typeof oneEventResult> }): JSX.Element {
  return <CalendarEvent event={result} />;
}

export const calendarEventResult: ResultPreview = defineResultPreview(oneEventResult, EventResult);

const eventPageResult = z
  .object({ events: z.array(calendarEvent).nullable().optional(), next_page_token: z.string().nullable().optional() })
  .passthrough();

function EventsResult({ result }: { result: z.infer<typeof eventPageResult> }): JSX.Element {
  const events = result.events ?? [];
  return (
    <Stack gap="md">
      {events.length === 0 ? (
        <Text size="sm" c="dimmed">
          No events.
        </Text>
      ) : (
        events.map((event) => <CalendarEvent key={event.event_id} event={event} />)
      )}
      {result.next_page_token && (
        <Text size="xs" c="dimmed">
          More events available.
        </Text>
      )}
    </Stack>
  );
}

export const calendarEventsResult: ResultPreview = defineResultPreview(eventPageResult, EventsResult);
