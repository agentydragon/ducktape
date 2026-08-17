// `create_event`'s pending and finished states are one evolving view: CreateCalendarEventPreview
// (requests.tsx) while the call is pending, CalendarEventResultView (responses.tsx) — the same full
// event view get_event/list_events use — once it has executed. A failed call keeps rendering the
// pending view; there is nothing to link to yet, and the card's error line (tool_call_card.tsx)
// already shows the message.
import { defineCallPreview, type ToolCallPreview } from "../call_entry";
import { CreateCalendarEventPreview, type CreateCalendarEventArgs, zCreateCalendarEventArgs } from "./requests";
import { CalendarEventResultView, type CalendarEvent, zCreateEventResult } from "./responses";

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
  create_event: defineCallPreview(zCreateCalendarEventArgs, zCreateEventResult, CreateEventCall),
} satisfies Record<string, ToolCallPreview>;
