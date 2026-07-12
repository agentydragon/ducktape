import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry.tsx";
import { googleCalendarResultPreviews } from "./responses.tsx";

describe("googleCalendarResultPreviews", () => {
  it("renders create_calendar_event for the Python-field-name spelling, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(
          googleCalendarResultPreviews.create_calendar_event,
          { event_id: "evt1", html_link: "https://www.google.com/calendar/event?eid=evt1" },
          variant
        )
      ).not.toBeNull();
    }
  });

  it("accepts the Calendar API's own id/htmlLink spelling and unknown extra keys", () => {
    expect(
      renderResultPreview(
        googleCalendarResultPreviews.create_calendar_event,
        { id: "evt1", htmlLink: "https://www.google.com/calendar/event?eid=evt1", status: "confirmed" },
        "detailed"
      )
    ).not.toBeNull();
  });

  it("returns null when no event link came back (→ raw JSON fallback)", () => {
    expect(
      renderResultPreview(googleCalendarResultPreviews.create_calendar_event, { event_id: "evt1" }, "compact")
    ).toBeNull();
  });
});
