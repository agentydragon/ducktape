// `create_event`'s pending and finished states are one evolving view, not two independent
// widgets: reuses CreateCalendarEventPreview (requests.tsx) verbatim while the call is pending —
// summary/when/recurrence/location/etc, everything the operator is being asked to approve — and
// CreateCalendarEventResultView (responses.tsx) once it has actually executed — a linked title
// (Google Calendar's own icon marks it as external) plus the event id/status in detailed. The
// card's error line (tool_call_card.tsx) already shows a failed call's message, so a failed/
// pending call keeps rendering the pending view — there's nothing to link to yet.
import { defineCallPreview, type ToolCallPreview } from "../call_entry.tsx";
import { CreateCalendarEventPreview, type CreateCalendarEventArgs, zCreateCalendarEventArgs } from "./requests.tsx";
import { CreateCalendarEventResultView, type CalendarEvent, zCreateEventResult } from "./responses.tsx";

function CreateEventCall({
  args,
  result,
  variant,
}: {
  args: CreateCalendarEventArgs;
  result: CalendarEvent | undefined;
  variant: "compact" | "detailed";
}) {
  if (result) return <CreateCalendarEventResultView result={result} variant={variant} />;
  return <CreateCalendarEventPreview args={args} variant={variant} />;
}

/** Combined pending/finished widgets for the `google_calendar` server. */
export const googleCalendarCallPreviews = {
  create_event: defineCallPreview(zCreateCalendarEventArgs, zCreateEventResult, CreateEventCall, (args) => ({
    text: args.recurrence?.length ? "Google Calendar: Create recurring event" : "Google Calendar: Create event",
  })),
} satisfies Record<string, ToolCallPreview>;
