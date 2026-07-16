import type { McpToolResultFor } from "../../mcp_tool_result_schema.ts";

// Calendar-name lookup returned by google_calendar_calendar_summary for preview fixtures.
export const SAMPLE_CALENDAR_SUMMARY = {
  calendar_id: "family@group.calendar.google.com",
  summary: "Family",
  html_link: "https://calendar.google.com/calendar/u/0/r?cid=ZmFtaWx5QGdyb3VwLmNhbGVuZGFyLmdvb2dsZS5jb20",
} satisfies McpToolResultFor<"google_calendar", "calendar_summary">;

export const GOOGLE_CALENDAR_MCP_FIXTURES = {
  google_calendar_calendar_summary: () => SAMPLE_CALENDAR_SUMMARY,
};
