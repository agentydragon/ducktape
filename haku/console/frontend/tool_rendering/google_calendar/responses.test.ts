import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry.tsx";
import { googleCalendarResultPreviews } from "./responses.tsx";

describe("googleCalendarResultPreviews", () => {
  it("renders create_calendar_event for the wire shape, passing unknown extra keys through", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(
          googleCalendarResultPreviews.create_calendar_event,
          { event_id: "evt1", html_link: "https://www.google.com/calendar/event?eid=evt1", status: "confirmed" },
          variant
        )
      ).not.toBeNull();
    }
  });

  it("rejects the Calendar API's own id/htmlLink spelling — the wire is the Python field names", () => {
    // CreateCalendarEventResult's aliases are validation-only, so a camelCase payload here
    // would mean the backend contract changed; fall back to raw JSON rather than guess.
    expect(
      renderResultPreview(
        googleCalendarResultPreviews.create_calendar_event,
        { id: "evt1", htmlLink: "https://www.google.com/calendar/event?eid=evt1" },
        "detailed"
      )
    ).toBeNull();
  });

  it("returns null when no event link came back (→ raw JSON fallback)", () => {
    expect(
      renderResultPreview(googleCalendarResultPreviews.create_calendar_event, { event_id: "evt1" }, "compact")
    ).toBeNull();
  });
});
