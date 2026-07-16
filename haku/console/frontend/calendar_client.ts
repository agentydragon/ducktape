import { callOperatorMcpTool } from "./mcp_client.ts";
import { mcpToolResultSchema, type McpToolResultFor } from "./mcp_tool_result_schema.ts";

type CalendarEventsPage = McpToolResultFor<"google_calendar", "list_events">;

const zCalendarEventsPage = mcpToolResultSchema("google_calendar", "list_events");

export type CalendarSummary = {
  calendar_id: string;
  summary: string;
  html_link: string;
};

function calendarIdBase64(calendarId: string): string {
  const bytes = new TextEncoder().encode(calendarId);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/=+$/, "");
}

export function calendarSummaryFromEvents(calendarId: string, page: CalendarEventsPage): CalendarSummary {
  return {
    calendar_id: calendarId,
    summary: page.summary ?? calendarId,
    html_link: `https://calendar.google.com/calendar/u/0/r?cid=${calendarIdBase64(calendarId)}`,
  };
}

// Use the existing events.list tool to validate the calendar and obtain its standard `summary`
// field; the browser derives the stable calendar link from the id already present in the tool call.
export async function fetchCalendarSummary(calendarId: string): Promise<CalendarSummary> {
  const payload = await callOperatorMcpTool("google_calendar_list_events", {
    calendar_id: calendarId,
    max_results: 1,
  });
  return calendarSummaryFromEvents(calendarId, zCalendarEventsPage.parse(payload));
}
