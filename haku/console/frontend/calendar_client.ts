import { api, errorDetail } from "./client.ts";
import type { components } from "./api/schema";

export type CalendarSummary = components["schemas"]["CalendarSummary"];

// Live display-name + Google Calendar link for a calendar id, for rendering a pending
// create_calendar_event approval — the tool call's own arguments only carry the id.
export async function fetchCalendarSummary(calendarId: string): Promise<CalendarSummary> {
  const { data, error } = await api.GET("/api/google-calendar/calendar-summary", {
    params: { query: { calendar_id: calendarId } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load calendar name"));
  return data;
}
