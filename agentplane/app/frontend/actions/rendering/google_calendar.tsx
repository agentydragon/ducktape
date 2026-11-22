import { Anchor, Badge, Code, Group, Paper, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import {
  GoogleCalendarEventScope,
  type GoogleCalendarEventTime,
} from "../../../../action_service/api/contracts/operator_action_service_pb";
import { GoogleCalendarIcon } from "../../ui/brand_icons";
import { getGoogleCalendarPreview } from "../client";
import { definePreview, defineReceiptPreview, type PreviewProps } from "./entry";
import {
  eventTime,
  formatDateBound,
  formatEventRange,
  formatEventTime,
  formatRecurrence,
  type EventTime,
} from "./google_calendar_time";
import type { ActionPresentation } from "./presentation";
import { defineResultPreview } from "./result_entry";

const calendar = { calendar_id: z.string().min(1).optional() };
const eventTarget = z.strictObject({ ...calendar, event_id: z.string().min(1) });
const reminder = z.strictObject({
  method: z.enum(["popup", "email"]).optional(),
  minutes_before_start: z.int().min(0).max(40_320),
});
const changeFields = {
  description: z.string().nullish(),
  location: z.string().nullish(),
  attendees: z.array(z.string()).nullish(),
  reminders: z.array(reminder).nullish(),
  recurrence: z.array(z.string().startsWith("RRULE:")).min(1).nullish(),
};
const submittedTime = eventTime.refine((value) => value.dateTime === undefined || value.timeZone !== undefined);
const createArguments = z.strictObject({
  ...calendar,
  ...changeFields,
  summary: z.string(),
  start: submittedTime,
  end: submittedTime,
});
const compactEventTime = z.union([
  z.strictObject({ date: z.iso.date() }),
  z.strictObject({
    date_time: z.iso.datetime({ offset: true, precision: 0 }).regex(/:00(?:Z|[+-]\d\d:\d\d)$/),
    time_zone: z.string().min(1),
  }),
]);
const compactCreateArguments = z.strictObject({
  calendar_id: z.literal("primary").optional(),
  summary: z.string().trim().min(1).max(120),
  start: compactEventTime,
  end: compactEventTime,
});
const canReviewCreateCompactly = (args: unknown): boolean => compactCreateArguments.safeParse(args).success;
const updateArguments = eventTarget.extend({
  ...changeFields,
  summary: z.string().nullish(),
  start: submittedTime.nullish(),
  end: submittedTime.nullish(),
});
const pageArguments = {
  ...calendar,
  time_min: z.string().nullish(),
  time_max: z.string().nullish(),
  max_results: z.int().min(1).max(250).optional(),
  page_token: z.string().nullish(),
};
const listArguments = z.strictObject({
  ...pageArguments,
  query: z.string().nullish(),
  expand_recurring: z.boolean().optional(),
});
const instanceArguments = z.strictObject({ ...pageArguments, recurring_event_id: z.string().min(1) });

const participant = z.object({
  email: z.string().nullish(),
  display_name: z.string().nullish(),
});
const event = z.object({
  event_id: z.string().min(1),
  summary: z.string().nullish(),
  html_link: z.string().nullish(),
  status: z.string().nullish(),
  location: z.string().nullish(),
  description: z.string().nullish(),
  recurrence: z.array(z.string()).optional(),
  recurring_event_id: z.string().nullish(),
  original_start_time: eventTime.nullish(),
  start: eventTime.nullish(),
  end: eventTime.nullish(),
  organizer: participant.nullish(),
  attendees: z
    .array(
      participant.extend({ email: z.string(), response_status: z.string().nullish(), optional: z.boolean().optional() })
    )
    .optional(),
  reminders: z.object({ use_default: z.boolean(), overrides: z.array(reminder).optional() }).nullish(),
});
const eventPage = z.object({
  events: z.array(event),
  summary: z.string().nullish(),
  next_page_token: z.string().nullish(),
});

function Field({ label, children }: { label?: string; children: ReactNode }): JSX.Element {
  return (
    <Text component="div" size="sm" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
      {label && (
        <>
          <Text span fw={600}>
            {label}
          </Text>
          {" · "}
        </>
      )}
      {children}
    </Text>
  );
}

function CalendarName({ id, name }: { id?: string; name?: string | null }): JSX.Element {
  if (!id || id === "primary") return <>{name?.trim() ? name : "Primary calendar"}</>;
  if (!name?.trim()) return <Code>{id}</Code>;
  // Match the service's 1024-byte calendar ID limit even for links built from recorded results.
  if (id.length > 1024) return <>{name}</>;
  const bytes = new TextEncoder().encode(id);
  if (bytes.length > 1024) return <>{name}</>;
  // The calendar target is explicit. Let Google select the browser's account rather than pinning
  // an account slot, which need not match the credential used by the integration.
  const url = new URL("https://calendar.google.com/calendar");
  url.searchParams.set("cid", btoa(String.fromCharCode(...bytes)).replace(/=+$/, ""));
  return (
    <Anchor href={url.href} target="_blank" rel="noreferrer" inherit>
      {name}
    </Anchor>
  );
}

function useCalendarPreview(requestId?: string) {
  const { data } = useQuery({
    queryKey: ["action-google-calendar-preview", requestId],
    queryFn: ({ signal }) => (requestId === undefined ? null : getGoogleCalendarPreview(requestId, signal)),
    enabled: requestId !== undefined,
    retry: false,
    staleTime: 30_000,
  });
  return data;
}

function CalendarTarget({
  id,
  requestId,
  compact,
}: {
  id?: string;
  requestId?: string;
  compact?: boolean;
}): JSX.Element {
  const calendar = useCalendarPreview(id && id !== "primary" ? requestId : undefined)?.calendar;
  if (compact) {
    return (
      <Field label="Calendar">
        {(calendar?.calendarId === id && calendar?.summary?.trim()) ||
          (id && id !== "primary" ? id : "Primary calendar")}
      </Field>
    );
  }
  return <CalendarField id={id} name={calendar?.calendarId === id ? calendar?.summary : undefined} />;
}

function CalendarField({ id, name }: { id?: string; name?: string | null }): JSX.Element {
  return (
    <Field label="Calendar">
      <CalendarName id={id} name={name} />
    </Field>
  );
}

function calendarEventUrl(link?: string | null): string | undefined {
  if (!link) return undefined;
  try {
    const url = new URL(link);
    if (
      (url.origin === "https://calendar.google.com" || url.origin === "https://www.google.com") &&
      url.pathname.startsWith("/calendar/") &&
      !url.username &&
      !url.password
    ) {
      return url.href;
    }
  } catch {
    // Unexpected links remain available in Raw.
  }
  return undefined;
}

function EventName({ id, name, link }: { id: string; name?: string | null; link?: string | null }): JSX.Element {
  const href = calendarEventUrl(link);
  const title = name?.trim() ? name : <Code>{id}</Code>;
  return href ? (
    <Anchor href={href} target="_blank" rel="noreferrer" inherit>
      <GoogleCalendarIcon />
      {title}
    </Anchor>
  ) : (
    <>{title}</>
  );
}

function previewTime(value: GoogleCalendarEventTime | undefined): EventTime | undefined {
  const parsed = eventTime.safeParse(
    value && {
      date: value.date || undefined,
      dateTime: value.dateTime || undefined,
      timeZone: value.timeZone || undefined,
    }
  );
  return parsed.success ? parsed.data : undefined;
}

function scopeLabel(scope?: GoogleCalendarEventScope): string | undefined {
  switch (scope) {
    case GoogleCalendarEventScope.SINGLE_EVENT:
      return "One event";
    case GoogleCalendarEventScope.OCCURRENCE:
      return "Only this occurrence";
    case GoogleCalendarEventScope.SERIES:
      return "Entire recurring series";
    default:
      return undefined;
  }
}

function Target({
  id,
  calendarId,
  recordedCalendarName,
  requestId,
  kind,
  showEvent = true,
  compact = false,
}: {
  id: string;
  calendarId?: string;
  recordedCalendarName?: string | null;
  requestId?: string;
  kind: "read" | "change" | "series";
  showEvent?: boolean;
  compact?: boolean;
}): JSX.Element {
  const data = useCalendarPreview(requestId);
  const preview = data?.event?.eventId === id ? data.event : undefined;
  const calendar = data?.calendar;
  const schedule = preview && formatEventRange(previewTime(preview.start), previewTime(preview.end));
  const original = previewTime(preview?.originalStartTime);
  const scope =
    kind === "change" && requestId
      ? (scopeLabel(preview?.scope) ??
        "The recorded ID determines whether this affects one event, an occurrence, or a series.")
      : undefined;
  return (
    <Stack gap={4}>
      {showEvent && (
        // A linked event name carries the Calendar icon, which already says what it is.
        <Field
          label={kind === "series" ? "Series" : compact || !calendarEventUrl(preview?.htmlLink) ? "Event" : undefined}
        >
          {compact ? (
            preview?.summary?.trim() || id
          ) : (
            <EventName id={id} name={preview?.summary} link={preview?.htmlLink} />
          )}
        </Field>
      )}
      {compact ? (
        <CalendarTarget id={calendarId} requestId={requestId} compact />
      ) : (
        <CalendarField
          id={calendarId}
          name={recordedCalendarName ?? (calendar?.calendarId === calendarId ? calendar?.summary : undefined)}
        />
      )}
      {schedule && <Field label="Current time">{schedule}</Field>}
      {original && <Field label="Original occurrence">{formatEventTime(original)}</Field>}
      {scope && <Field label="Scope">{scope}</Field>}
    </Stack>
  );
}

function remindersText(reminders: z.infer<typeof reminder>[]): string {
  if (reminders.length === 0) return "Use calendar defaults";
  return reminders
    .map(({ method, minutes_before_start }) => {
      const when = minutes_before_start === 0 ? "at start" : `${minutes_before_start} min before`;
      return `${method === "email" ? "Email" : "Pop-up"} ${when}`;
    })
    .join(" · ");
}

function Recurrence({ lines, start }: { lines: string[]; start?: EventTime | null }): JSX.Element {
  return <Field label="Repeats">{lines.map((line) => formatRecurrence(line, start)).join("\n")}</Field>;
}

const createLabel = definePreview(createArguments, ({ args }) =>
  args.recurrence ? "Schedule recurring Calendar event" : "Schedule Calendar event"
);
const createSummary = definePreview(createArguments, ({ args, requestId }) => (
  <Stack gap={2}>
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      {args.summary || "Untitled event"}
    </Text>
    <Text size="xs">{formatEventRange(args.start, args.end)}</Text>
    <CalendarTarget id={args.calendar_id} requestId={requestId} compact />
    {args.location && <Field label="Location">{args.location}</Field>}
    {!!args.attendees?.length && <Field label="Attendees">{args.attendees.join(", ")}</Field>}
    {args.recurrence && <Recurrence lines={args.recurrence} start={args.start} />}
    <Field label="Reminders">{remindersText(args.reminders ?? [])}</Field>
    {args.description && (
      <Text size="xs" c="dimmed">
        Includes a description
      </Text>
    )}
  </Stack>
));
const createDetails = definePreview(createArguments, ({ args, requestId }) => (
  <Stack gap={4}>
    <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
      {args.summary || "Untitled event"}
    </Text>
    <Field label="When">{formatEventRange(args.start, args.end)}</Field>
    <CalendarTarget id={args.calendar_id} requestId={requestId} />
    {args.location && <Field label="Location">{args.location}</Field>}
    {args.attendees && args.attendees.length > 0 && <Field label="Attendees">{args.attendees.join(", ")}</Field>}
    <Field label="Reminders">{remindersText(args.reminders ?? [])}</Field>
    {args.recurrence && <Recurrence lines={args.recurrence} start={args.start} />}
    {args.description && <Field label="Description">{args.description}</Field>}
  </Stack>
));

function Update({
  args,
  requestId,
  showEvent,
  compact,
}: PreviewProps<z.infer<typeof updateArguments>> & { showEvent?: boolean; compact?: boolean }): JSX.Element {
  const changes = [
    args.summary != null && (
      <Field key="title" label="Title">
        {args.summary || "Clear title"}
      </Field>
    ),
    (args.start || args.end) && (
      <Field key="when" label="New time">
        {formatEventRange(args.start, args.end)}
      </Field>
    ),
    args.location != null && (
      <Field key="location" label="Location">
        {args.location || "Clear location"}
      </Field>
    ),
    args.attendees != null && (
      <Field key="attendees" label="Attendees">
        {args.attendees.length > 0 ? `Replace with ${args.attendees.join(", ")}` : "Remove all attendees"}
      </Field>
    ),
    args.reminders != null && (
      <Field key="reminders" label="Reminders">
        {remindersText(args.reminders)}
      </Field>
    ),
    args.recurrence != null && <Recurrence key="recurrence" lines={args.recurrence} start={args.start} />,
    args.description != null && (
      <Field key="description" label="Description">
        {args.description ? (compact ? "Replace description" : args.description) : "Clear description"}
      </Field>
    ),
  ].filter(Boolean);
  return (
    <Stack gap="xs">
      <Target
        id={args.event_id}
        calendarId={args.calendar_id}
        requestId={requestId}
        kind="change"
        showEvent={showEvent}
        compact={compact}
      />
      <Stack gap={4}>
        <Text size="sm" fw={600}>
          {changes.length === 0 ? "No field changes requested" : "Requested changes"}
        </Text>
        {changes}
      </Stack>
    </Stack>
  );
}

function EventTarget({ args, requestId }: PreviewProps<z.infer<typeof eventTarget>>): JSX.Element {
  return <Target id={args.event_id} calendarId={args.calendar_id} requestId={requestId} kind="read" />;
}

function Delete({ args, requestId }: PreviewProps<z.infer<typeof eventTarget>>): JSX.Element {
  return <Target id={args.event_id} calendarId={args.calendar_id} requestId={requestId} kind="change" />;
}

function dateWindow(args: z.infer<typeof listArguments>): JSX.Element {
  return (
    <>
      {args.time_min && <Field label="Ending after">{formatDateBound(args.time_min)}</Field>}
      {args.time_max && <Field label="Starting before">{formatDateBound(args.time_max)}</Field>}
      {args.max_results !== undefined && <Field label="Results per page">{args.max_results}</Field>}
      {args.page_token && (
        <Text size="sm" c="dimmed">
          Continue from an earlier page
        </Text>
      )}
    </>
  );
}

function ListDetails({
  args,
  requestId,
  recordedCalendarName,
}: PreviewProps<z.infer<typeof listArguments>> & { recordedCalendarName?: string | null }): JSX.Element {
  return (
    <Stack gap={4}>
      {recordedCalendarName ? (
        <CalendarField id={args.calendar_id} name={recordedCalendarName} />
      ) : (
        <CalendarTarget id={args.calendar_id} requestId={requestId} />
      )}
      {args.query && <Field label="Search">{args.query}</Field>}
      {dateWindow(args)}
      <Field label="Recurring events">
        {args.expand_recurring ? "Expand into occurrences" : "Include series and exceptions"}
      </Field>
    </Stack>
  );
}

function InstanceDetails({
  args,
  requestId,
  recordedCalendarName,
}: PreviewProps<z.infer<typeof instanceArguments>> & { recordedCalendarName?: string | null }): JSX.Element {
  return (
    <Stack gap={4}>
      <Target
        id={args.recurring_event_id}
        calendarId={args.calendar_id}
        requestId={requestId}
        recordedCalendarName={recordedCalendarName}
        kind="series"
      />
      {dateWindow(args)}
    </Stack>
  );
}

function eventStatus(value?: string | null): string | undefined {
  switch (value) {
    case "cancelled":
      return "Cancelled";
    case "tentative":
      return "Tentative";
    case "confirmed":
    case undefined:
    case null:
      return undefined;
    default:
      return value;
  }
}

function participantName(value: z.infer<typeof participant>): string {
  if (value.display_name && value.email && value.display_name !== value.email) {
    return `${value.display_name} (${value.email})`;
  }
  return value.display_name || value.email || "";
}

function responseLabel(value: string): string {
  switch (value) {
    case "accepted":
      return "Accepted";
    case "declined":
      return "Declined";
    case "tentative":
      return "Maybe";
    case "needsAction":
      return "Awaiting reply";
    default:
      return value;
  }
}

function EventCard({ value, agenda = false }: { value: z.infer<typeof event>; agenda?: boolean }): JSX.Element {
  const status = eventStatus(value.status);
  const original = value.original_start_time;
  const time = formatEventRange(value.start, value.end);
  const organiser = value.organizer && participantName(value.organizer);
  return (
    <Paper withBorder p="sm">
      <Stack gap={4}>
        <Group justify="space-between" align="flex-start" gap="xs">
          <Text size="sm" fw={600} style={{ overflowWrap: "anywhere", minWidth: 0 }}>
            <EventName id={value.event_id} name={value.summary} link={value.html_link} />
          </Text>
          {status && (
            <Badge size="sm" variant="light" color={value.status === "cancelled" ? "gray" : "yellow"}>
              {status}
            </Badge>
          )}
        </Group>
        {time && <Text size="sm">{time}</Text>}
        {original && (time === undefined || !agenda) && (
          <Field label="Original occurrence">{formatEventTime(original)}</Field>
        )}
        {value.location && <Field label="Location">{value.location}</Field>}
        {value.recurrence && value.recurrence.length > 0 && <Recurrence lines={value.recurrence} start={value.start} />}
        {!agenda && (
          <>
            {organiser && <Field label="Organizer">{organiser}</Field>}
            {value.attendees && value.attendees.length > 0 && (
              <Stack gap={2}>
                <Text size="sm" fw={600}>
                  Attendees
                </Text>
                {value.attendees.map((person) => (
                  <Text key={person.email} size="sm" style={{ overflowWrap: "anywhere" }}>
                    {participantName(person)}
                    {person.optional && " · Optional"}
                    {person.response_status && ` · ${responseLabel(person.response_status)}`}
                  </Text>
                ))}
              </Stack>
            )}
            {value.reminders && (
              <Field label="Reminders">
                {value.reminders.use_default
                  ? "Calendar defaults"
                  : value.reminders.overrides?.length
                    ? remindersText(value.reminders.overrides)
                    : "No reminders"}
              </Field>
            )}
            {value.description && <Field label="Description">{value.description}</Field>}
          </>
        )}
      </Stack>
    </Paper>
  );
}

const eventResult = defineResultPreview(event, ({ result }) => <EventCard value={result} />);

function Agenda({ result }: { result: z.infer<typeof eventPage> }): JSX.Element {
  return (
    <Stack gap="xs">
      {result.events.length === 0 && (
        <Text size="sm" c="dimmed">
          No events on this page.
        </Text>
      )}
      {result.events.map((item) => (
        <EventCard key={item.event_id} value={item} agenda />
      ))}
      {result.next_page_token && (
        <Text size="sm" c="dimmed">
          More events are available.
        </Text>
      )}
    </Stack>
  );
}

const agendaArguments = z.union([listArguments, instanceArguments]);
const agendaResult = defineResultPreview(eventPage, ({ result, context }) => {
  const args = agendaArguments.safeParse(context?.arguments);
  return (
    <Stack gap="xs">
      {result.summary?.trim() && (
        <Text size="sm" fw={600}>
          <CalendarName id={args.data?.calendar_id} name={result.summary} />
        </Text>
      )}
      <Agenda result={result} />
    </Stack>
  );
});

function CalendarReceipt({ review, children }: { review: ReactNode; children: ReactNode }): JSX.Element {
  return (
    <Stack gap="sm">
      {review}
      <Stack gap={4}>
        <Text size="sm" fw={600}>
          Result
        </Text>
        {children}
      </Stack>
    </Stack>
  );
}

function namesEvent(id: string, result: z.infer<typeof event>): boolean {
  return result.event_id === id && Boolean(result.summary?.trim());
}

const getEventCall = defineReceiptPreview(eventTarget, event, ({ args, result }) => (
  <CalendarReceipt
    review={
      <Target
        id={args.event_id}
        calendarId={args.calendar_id}
        kind="read"
        showEvent={!namesEvent(args.event_id, result)}
      />
    }
  >
    <EventCard value={result} />
  </CalendarReceipt>
));

const updateEventCall = defineReceiptPreview(updateArguments, event, ({ args, result }) => (
  <CalendarReceipt review={<Update args={args} showEvent={!namesEvent(args.event_id, result)} />}>
    <EventCard value={result} />
  </CalendarReceipt>
));

const namedEventPage = eventPage.refine((page) => Boolean(page.summary?.trim()));
const listEventsCall = defineReceiptPreview(listArguments, namedEventPage, ({ args, result }) => (
  <CalendarReceipt review={<ListDetails args={args} recordedCalendarName={result.summary} />}>
    <Agenda result={result} />
  </CalendarReceipt>
));

const listInstancesCall = defineReceiptPreview(instanceArguments, namedEventPage, ({ args, result }) => (
  <CalendarReceipt review={<InstanceDetails args={args} recordedCalendarName={result.summary} />}>
    <Agenda result={result} />
  </CalendarReceipt>
));

const updateDetails = definePreview(updateArguments, Update);
const updateSummary = definePreview(updateArguments, (props) => <Update {...props} compact />);
const eventDetails = definePreview(eventTarget, EventTarget);
const deleteDetails = definePreview(eventTarget, Delete);
const listDetails = definePreview(listArguments, ListDetails);
const instanceDetails = definePreview(instanceArguments, InstanceDetails);
const deleteSummary = definePreview(eventTarget, ({ args, requestId }) => (
  <Target id={args.event_id} calendarId={args.calendar_id} requestId={requestId} kind="change" compact />
));
const eventSummary = definePreview(eventTarget, ({ args, requestId }) => (
  <Target id={args.event_id} calendarId={args.calendar_id} requestId={requestId} kind="read" compact />
));
const listSummary = definePreview(listArguments, ({ args, requestId }) => (
  <Stack gap={4}>
    <CalendarTarget id={args.calendar_id} requestId={requestId} compact />
    {args.query && <Field label="Search">{args.query}</Field>}
    {dateWindow(args)}
  </Stack>
));
const instanceSummary = definePreview(instanceArguments, ({ args, requestId }) => (
  <Stack gap={4}>
    <Target id={args.recurring_event_id} calendarId={args.calendar_id} requestId={requestId} kind="series" compact />
    {dateWindow(args)}
  </Stack>
));

export const googleCalendarRenderers: ReadonlyMap<string, ActionPresentation> = new Map<string, ActionPresentation>([
  [
    "create_event",
    {
      fallbackLabel: "Schedule Calendar event",
      label: createLabel,
      pane: { collapsed: createSummary, opened: createDetails },
      details: { arguments: createDetails, result: eventResult, pendingPreviewOnly: true },
      offersCardDecisionButtons: canReviewCreateCompactly,
      compactCardExpands: (args) => !canReviewCreateCompactly(args),
    },
  ],
  [
    "update_event",
    {
      fallbackLabel: "Update Calendar event",
      call: updateEventCall,
      pane: { collapsed: updateSummary, opened: updateDetails },
      details: { arguments: updateDetails, result: eventResult, pendingPreviewOnly: true },
    },
  ],
  [
    "delete_event",
    {
      fallbackLabel: "Delete Calendar event",
      pane: { collapsed: deleteSummary, opened: deleteDetails },
      details: {
        arguments: deleteDetails,
        pendingPreviewOnly: true,
        emptyResult: <Text size="sm">Deletion confirmed.</Text>,
      },
    },
  ],
  [
    "get_event",
    {
      fallbackLabel: "Get Calendar event",
      call: getEventCall,
      pane: { collapsed: eventSummary, opened: eventDetails },
      details: { arguments: eventDetails, result: eventResult, pendingPreviewOnly: true },
    },
  ],
  [
    "list_events",
    {
      fallbackLabel: "List Calendar events",
      call: listEventsCall,
      pane: { collapsed: listSummary, opened: listDetails },
      details: { arguments: listDetails, result: agendaResult, pendingPreviewOnly: true },
    },
  ],
  [
    "list_event_instances",
    {
      fallbackLabel: "List Calendar series occurrences",
      call: listInstancesCall,
      pane: { collapsed: instanceSummary, opened: instanceDetails },
      details: { arguments: instanceDetails, result: agendaResult, pendingPreviewOnly: true },
    },
  ],
]);
