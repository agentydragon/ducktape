// `create_event`'s pending and finished states are one evolving view, not two independent
// widgets: reuses CreateCalendarEventPreview (requests.tsx) verbatim while the call is pending —
// summary/when/recurrence/location/etc, everything the operator is being asked to approve — and
// CalendarEventResultView (responses.tsx) once it has actually executed — the same full event
// view get_event/list_events use (when/recurrence/location/etc, plus the id/status in detailed).
// The combined widget only ever renders one of the two at a time, so this isn't a double-listing
// the way it would be if both rendered together. The card's error line (tool_call_card.tsx)
// already shows a failed call's message, so a failed/pending call keeps rendering the pending
// view — there's nothing to link to yet.
import { defineCallPreview, type ToolCallPreview } from "../call_entry.tsx";
import { CreateCalendarEventPreview, type CreateCalendarEventArgs, zCreateCalendarEventArgs } from "./requests.tsx";
import { CalendarEventResultView, type CalendarEvent, zCreateEventResult } from "./responses.tsx";

function CreateEventCall({
  args,
  result,
  variant,
}: {
  args: CreateCalendarEventArgs;
  result: CalendarEvent | undefined;
  variant: "compact" | "detailed";
}) {
  if (result) return <CalendarEventResultView result={result} variant={variant} />;
  return <CreateCalendarEventPreview args={args} variant={variant} />;
}

/** Combined pending/finished widgets for the `google_calendar` server. */
export const googleCalendarCallPreviews = {
  create_event: defineCallPreview(zCreateCalendarEventArgs, zCreateEventResult, CreateEventCall, (args) => ({
    text: args.recurrence?.length ? "Google Calendar: Create recurring event" : "Google Calendar: Create event",
  })),
} satisfies Record<string, ToolCallPreview>;
