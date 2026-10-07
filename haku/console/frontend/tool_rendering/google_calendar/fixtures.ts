import type { McpToolResultFor } from "../../mcp_tool_result_schema";

// The existing events.list result includes the calendar's standard display summary.
const FAMILY_CALENDAR_EVENTS: McpToolResultFor<"google_calendar", "list_events"> = {
  events: [],
  next_page_token: null,
  summary: "Family",
};

export const GOOGLE_CALENDAR_MCP_FIXTURES = {
  google_calendar__list_events: (): typeof FAMILY_CALENDAR_EVENTS => FAMILY_CALENDAR_EVENTS,
};
