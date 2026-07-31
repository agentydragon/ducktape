import type { McpToolResultFor } from "../../mcp_tool_result_schema";

// The existing events.list result includes the calendar's standard display summary.
const FAMILY_CALENDAR_EVENTS = {
  events: [],
  next_page_token: null,
  summary: "Family",
} satisfies McpToolResultFor<"google_calendar", "list_events">;

export const GOOGLE_CALENDAR_MCP_FIXTURES = {
  google_calendar__list_events: () => FAMILY_CALENDAR_EVENTS,
};
