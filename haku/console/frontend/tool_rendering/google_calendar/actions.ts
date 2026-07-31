// Notification/card action descriptions for Google Calendar's tools — the one-line summary the
// approvals card shows and a push notification is titled with. Beside the widgets they
// describe, and React-free so `../../sw.ts` can bundle them (see ../action_entry.ts).

import { mcpToolSchema } from "../../mcp_tool_schema";
import { fixed, fromArgs } from "../action_entry";
import type { ActionEntry } from "../action_entry";
import { GOOGLE_CALENDAR_SERVER_ID } from "../server_ids";

export const googleCalendarActions: Record<string, ActionEntry> = {
  create_event: fromArgs(mcpToolSchema(GOOGLE_CALENDAR_SERVER_ID, "create_event"), (a) => ({
    text: a.recurrence?.length ? "Google Calendar: Create recurring event" : "Google Calendar: Create event",
  })),
  get_event: fixed("Google Calendar: Get event"),
  list_events: fixed("Google Calendar: List events"),
  list_event_instances: fixed("Google Calendar: List event instances"),
};
