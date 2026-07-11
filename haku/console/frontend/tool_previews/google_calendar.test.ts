import { describe, expect, it } from "vitest";

import { googleCalendarToolPreview } from "./google_calendar.tsx";

describe("googleCalendarToolPreview", () => {
  it("renders create_calendar_event for a valid all-day event, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const preview = googleCalendarToolPreview(
        "create_calendar_event",
        { summary: "Standup", start: { date: "2026-09-15" }, end: { date: "2026-09-16" } },
        variant
      );
      expect(preview).not.toBeNull();
      expect(preview).not.toBe(false);
    }
  });

  it("returns null when args are missing required fields", () => {
    expect(googleCalendarToolPreview("create_calendar_event", { summary: "no start/end" }, "detailed")).toBeNull();
  });

  it("returns null for a tool it doesn't render", () => {
    expect(googleCalendarToolPreview("list_events", {}, "detailed")).toBeNull();
  });
});
