// Per-tool-type rendering for haku-console's native `google` MCP server (see
// haku/console/google_tools.py). Falls back to the generic raw-JSON view
// (approval_state.ts's argumentsJson) for anything that isn't shaped as expected —
// arguments are only validated by the tool's own Pydantic model at execution time, not
// at submission, so a pending approval's arguments could in principle be malformed.

import { Anchor, Badge, Group, Loader, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import { fetchGmailThreadPreviews, type GmailThreadPreview } from "./client.ts";
import { Field } from "./console_panel.tsx";

const GOOGLE_SERVER_ID = "google";

interface EventDateTime {
  date?: string | null;
  date_time?: string | null;
  time_zone?: string | null;
}

interface CalendarReminder {
  method: "popup" | "email";
  minutes_before_start: number;
}

interface CreateCalendarEventArgs {
  summary: string;
  start: EventDateTime;
  end: EventDateTime;
  description?: string | null;
  location?: string | null;
  calendar_id?: string;
  reminders?: CalendarReminder[];
  attendees?: string[];
}

interface BatchModifyGmailThreadLabelsArgs {
  thread_ids: string[];
  add?: string[];
  remove?: string[];
}

interface CreateGmailDraftArgs {
  to: string[];
  subject: string;
  body: string;
  cc?: string[];
  thread_id?: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((v) => typeof v === "string");
}

function isEventDateTime(value: unknown): value is EventDateTime {
  if (!isRecord(value)) return false;
  const dateOk = value.date === undefined || value.date === null || typeof value.date === "string";
  const dateTimeOk = value.date_time === undefined || value.date_time === null || typeof value.date_time === "string";
  return dateOk && dateTimeOk;
}

function asCreateCalendarEventArgs(args: Record<string, unknown>): CreateCalendarEventArgs | null {
  if (typeof args.summary !== "string" || !isEventDateTime(args.start) || !isEventDateTime(args.end)) return null;
  return args as unknown as CreateCalendarEventArgs;
}

function asBatchModifyGmailThreadLabelsArgs(args: Record<string, unknown>): BatchModifyGmailThreadLabelsArgs | null {
  if (!isStringArray(args.thread_ids)) return null;
  return args as unknown as BatchModifyGmailThreadLabelsArgs;
}

function asCreateGmailDraftArgs(args: Record<string, unknown>): CreateGmailDraftArgs | null {
  if (!isStringArray(args.to) || typeof args.subject !== "string" || typeof args.body !== "string") return null;
  return args as unknown as CreateGmailDraftArgs;
}

function formatEventDateTime(value: EventDateTime): string {
  if (value.date) return value.date;
  if (value.date_time) return value.time_zone ? `${value.date_time} (${value.time_zone})` : value.date_time;
  return "(unset)";
}

function formatReminder(reminder: CalendarReminder): string {
  const unit =
    reminder.minutes_before_start % 1440 === 0
      ? `${reminder.minutes_before_start / 1440} day(s)`
      : reminder.minutes_before_start % 60 === 0
        ? `${reminder.minutes_before_start / 60} hour(s)`
        : `${reminder.minutes_before_start} min`;
  return `${reminder.method === "popup" ? "Popup" : "Email"}, ${unit} before`;
}

function CreateCalendarEventPreview({ args }: { args: CreateCalendarEventArgs }) {
  return (
    <Stack gap="xs">
      <Field label="Event">{args.summary}</Field>
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
    </Stack>
  );
}

function GmailThreadRow({ threadId, preview }: { threadId: string; preview: GmailThreadPreview | undefined }) {
  if (!preview) {
    return (
      <Text size="sm" c="dimmed">
        {threadId} (couldn't load preview)
      </Text>
    );
  }
  return (
    <Stack gap={0}>
      <Anchor href={preview.gmail_url} target="_blank" rel="noreferrer" size="sm">
        {preview.subject}
      </Anchor>
      {preview.current_label_names.length > 0 && (
        <Text size="xs" c="dimmed">
          Current labels: {preview.current_label_names.join(", ")}
        </Text>
      )}
    </Stack>
  );
}

function BatchModifyGmailThreadLabelsPreview({ args }: { args: BatchModifyGmailThreadLabelsArgs }) {
  const [previews, setPreviews] = useState<Record<string, GmailThreadPreview> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setPreviews(null);
    setError(null);
    fetchGmailThreadPreviews(args.thread_ids)
      .then((result) => {
        if (alive) setPreviews(result);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
    // args.thread_ids is a fresh array reference per render of a JSON-derived value; the
    // approval itself is immutable once submitted, so join() is a stable enough dep key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [args.thread_ids.join(",")]);

  return (
    <Stack gap="xs">
      {(args.add?.length ?? 0) > 0 && (
        <Field label="Add labels">
          <Group gap={4}>
            {args.add?.map((name) => (
              <Badge key={name} variant="light" color="teal">
                {name}
              </Badge>
            ))}
          </Group>
        </Field>
      )}
      {(args.remove?.length ?? 0) > 0 && (
        <Field label="Remove labels">
          <Group gap={4}>
            {args.remove?.map((name) => (
              <Badge key={name} variant="light" color="red">
                {name}
              </Badge>
            ))}
          </Group>
        </Field>
      )}
      <Field label={`Threads (${args.thread_ids.length})`}>
        {error ? (
          <Text size="sm" c="red">
            {error}
          </Text>
        ) : previews === null ? (
          <Loader size="xs" />
        ) : (
          <Stack gap="xs">
            {args.thread_ids.map((threadId) => (
              <GmailThreadRow key={threadId} threadId={threadId} preview={previews[threadId]} />
            ))}
          </Stack>
        )}
      </Field>
    </Stack>
  );
}

function CreateGmailDraftPreview({ args }: { args: CreateGmailDraftArgs }) {
  return (
    <Stack gap="xs">
      <Field label="To">{args.to.join(", ")}</Field>
      {args.cc && args.cc.length > 0 && <Field label="Cc">{args.cc.join(", ")}</Field>}
      <Field label="Subject">{args.subject}</Field>
      <Field label="Body">
        <pre className="haku-shell-json">{args.body}</pre>
      </Field>
      {args.thread_id && <Field label="Replying within thread">{args.thread_id}</Field>}
    </Stack>
  );
}

/** Nice per-tool rendering for the `google` server's tools; `null` when the (server, tool,
 * arguments) triple doesn't match a known widget, so the caller falls back to raw JSON. */
export function googleToolPreview(serverId: string, toolName: string, args: Record<string, unknown>): ReactNode | null {
  if (serverId !== GOOGLE_SERVER_ID) return null;
  if (toolName === "create_calendar_event") {
    const parsed = asCreateCalendarEventArgs(args);
    return parsed && <CreateCalendarEventPreview args={parsed} />;
  }
  if (toolName === "batch_modify_gmail_thread_labels") {
    const parsed = asBatchModifyGmailThreadLabelsArgs(args);
    return parsed && <BatchModifyGmailThreadLabelsPreview args={parsed} />;
  }
  if (toolName === "create_gmail_draft") {
    const parsed = asCreateGmailDraftArgs(args);
    return parsed && <CreateGmailDraftPreview args={parsed} />;
  }
  return null;
}
