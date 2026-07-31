import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry";
import { googleCalendarResultPreviews } from "./responses";

const recurringEvent = {
  event_id: "evt1",
  status: "confirmed",
  summary: "Standup",
  start: { date_time: "2026-09-15T09:00:00-07:00", time_zone: "America/Los_Angeles" },
  end: { date_time: "2026-09-15T09:30:00-07:00", time_zone: "America/Los_Angeles" },
  recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12"],
  html_link: "https://www.google.com/calendar/event?eid=evt1",
};

describe("googleCalendarResultPreviews", () => {
  it("has no entry for create_event — it's a combined widget (calls.tsx) instead", () => {
    expect("create_event" in googleCalendarResultPreviews).toBe(false);
  });

  it("renders the focused event shape for get", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderResultPreview(googleCalendarResultPreviews.get_event, recurringEvent, variant)).not.toBeNull();
    }
  });

  it("renders event pages for list and instances", () => {
    const page = { events: [recurringEvent], next_page_token: "next-page" };
    expect(renderResultPreview(googleCalendarResultPreviews.list_events, page, "compact")).not.toBeNull();
    expect(renderResultPreview(googleCalendarResultPreviews.list_event_instances, page, "detailed")).not.toBeNull();
  });

  it("accepts a minimal cancelled instance", () => {
    expect(
      renderResultPreview(
        googleCalendarResultPreviews.get_event,
        { event_id: "cancelled1", status: "cancelled", recurring_event_id: "series1" },
        "detailed"
      )
    ).not.toBeNull();
  });

  it("rejects the Calendar API's camelCase resource shape", () => {
    expect(
      renderResultPreview(
        googleCalendarResultPreviews.get_event,
        { id: "evt1", htmlLink: "https://www.google.com/calendar/event?eid=evt1" },
        "detailed"
      )
    ).toBeNull();
  });
});
