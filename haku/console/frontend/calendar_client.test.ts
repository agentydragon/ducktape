import { describe, expect, it } from "vitest";

import { calendarSummaryFromEvents } from "./calendar_client";

describe("calendarSummaryFromEvents", () => {
  it("uses the standard events.list summary and derives the calendar link", () => {
    expect(calendarSummaryFromEvents("family@group.calendar.google.com", { events: [], summary: "Family" })).toEqual({
      calendar_id: "family@group.calendar.google.com",
      summary: "Family",
      html_link: "https://calendar.google.com/calendar/u/0/r?cid=ZmFtaWx5QGdyb3VwLmNhbGVuZGFyLmdvb2dsZS5jb20",
    });
  });

  it("falls back to the calendar id when events.list omits its summary", () => {
    expect(calendarSummaryFromEvents("calendar-id", { events: [] }).summary).toBe("calendar-id");
  });
});
