import { callOperatorMcpTool } from "./mcp_client.ts";
import { mcpToolResultSchema, type McpToolResultFor } from "./mcp_tool_result_schema.ts";

export type CalendarSummary = McpToolResultFor<"google_calendar", "calendar_summary">;

const zCalendarSummary = mcpToolResultSchema("google_calendar", "calendar_summary");

// Resolve a calendar id through the same MCP tool and operator credential used by the console.
export async function fetchCalendarSummary(calendarId: string): Promise<CalendarSummary> {
  const payload = await callOperatorMcpTool("google_calendar_calendar_summary", { calendar_id: calendarId });
  return zCalendarSummary.parse(payload);
}
