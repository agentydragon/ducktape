import { describe, expect, it } from "vitest";

import { renderPreview } from "./entry.tsx";
import { googleCalendarPreviews } from "./google_calendar.tsx";

describe("googleCalendarPreviews", () => {
  it("renders create_calendar_event for a valid all-day event, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const node = renderPreview(
        googleCalendarPreviews.create_calendar_event,
        { summary: "Standup", start: { date: "2026-09-15" }, end: { date: "2026-09-16" } },
        variant
      );
      expect(node).not.toBeNull();
    }
  });

  it("returns null when args are missing required fields", () => {
    expect(
      renderPreview(googleCalendarPreviews.create_calendar_event, { summary: "no start/end" }, "detailed")
    ).toBeNull();
  });
});
