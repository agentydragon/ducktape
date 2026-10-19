// `create_event`'s and `update_event`'s pending and finished states are each one evolving view:
// their own args preview (requests.tsx) while the call is pending, CalendarEventResultView
// (responses.tsx) — the same full event view get_event/list_events use — once it has executed. A
// failed call keeps rendering the pending view; there is nothing to link to yet, and the card's
// error line (tool_call_card.tsx) already shows the message.
import { defineCallPreview, type ToolCallPreview } from "../call_entry";
import {
  CreateCalendarEventPreview,
  type CreateCalendarEventArgs,
  UpdateCalendarEventPreview,
  type UpdateCalendarEventArgs,
  zCreateCalendarEventArgs,
  zUpdateCalendarEventArgs,
} from "./requests";
import { CalendarEventResultView, type CalendarEvent, zCreateEventResult, zUpdateEventResult } from "./responses";

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

function UpdateEventCall({
  args,
  result,
  variant,
}: {
  args: UpdateCalendarEventArgs;
  result: CalendarEvent | undefined;
  variant: "compact" | "detailed";
}) {
  if (result) return <CalendarEventResultView result={result} variant={variant} />;
  return <UpdateCalendarEventPreview args={args} variant={variant} />;
}

/** Combined pending/finished widgets for the `google_calendar` server. */
export const googleCalendarCallPreviews: {
  create_event: ToolCallPreview<typeof zCreateCalendarEventArgs, typeof zCreateEventResult>;
  update_event: ToolCallPreview<typeof zUpdateCalendarEventArgs, typeof zUpdateEventResult>;
} = {
  create_event: defineCallPreview(zCreateCalendarEventArgs, zCreateEventResult, CreateEventCall),
  update_event: defineCallPreview(zUpdateCalendarEventArgs, zUpdateEventResult, UpdateEventCall),
} satisfies Record<string, ToolCallPreview>;
