import { describe, expect, it } from "vitest";

import { renderPreview } from "../entry.tsx";
import { formatEventDateTimeRange, googleCalendarPreviews } from "./requests.tsx";

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

  it("keeps the custom preview when nullable FastMCP arguments are explicitly null", () => {
    expect(
      renderPreview(
        googleCalendarPreviews.create_calendar_event,
        {
          summary: "Standup",
          start: { date: "2026-09-15" },
          end: { date: "2026-09-16" },
          reminders: null,
          attendees: null,
        },
        "detailed"
      )
    ).not.toBeNull();
  });

  it("rejects unknown arguments instead of rendering a custom preview", () => {
    expect(
      renderPreview(
        googleCalendarPreviews.create_calendar_event,
        {
          summary: "Standup",
          start: { date: "2026-09-15" },
          end: { date: "2026-09-16" },
          unexpected: true,
        },
        "compact"
      )
    ).toBeNull();
  });
});

describe("formatEventDateTimeRange", () => {
  it("collapses a same-day timed event to one line (24h, weekday), full date on hover", () => {
    const { text, title } = formatEventDateTimeRange(
      { date_time: "2026-07-12T18:00:00" },
      { date_time: "2026-07-12T19:00:00" }
    );
    expect(text).toContain("18:00–19:00"); // one range, not two stamps
    expect(text).not.toContain("T"); // no raw ISO leaking through
    expect(title).toContain("2026"); // hover keeps the full precision incl. year
  });

  it("keeps both ends when the event spans days", () => {
    const { text } = formatEventDateTimeRange(
      { date_time: "2026-07-12T22:00:00" },
      { date_time: "2026-07-13T06:00:00" }
    );
    expect(text).toContain("22:00");
    expect(text).toContain("06:00");
  });

  it("shows a single all-day event inclusively", () => {
    const { text } = formatEventDateTimeRange({ date: "2026-07-12" }, { date: "2026-07-13" });
    expect(text).toContain("all day");
  });

  it("shows a multi-day all-day span with Google's exclusive end pulled back a day", () => {
    // Google's end date 07-15 is exclusive → the span is 12–14 inclusive.
    const { text } = formatEventDateTimeRange({ date: "2026-07-12" }, { date: "2026-07-15" });
    expect(text).toContain("all day");
    expect(text).toContain("12");
    expect(text).toContain("14");
    expect(text).not.toContain("15");
  });
});
