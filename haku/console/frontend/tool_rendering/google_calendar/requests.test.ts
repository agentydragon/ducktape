import { describe, expect, it } from "vitest";

import { describeAction, renderPreview } from "../entry.tsx";
import { formatEventDateTimeRange, googleCalendarPreviews, humanizeRRule } from "./requests.tsx";

describe("googleCalendarPreviews", () => {
  it("renders create_event for a valid recurring all-day event, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const node = renderPreview(
        googleCalendarPreviews.create_event,
        {
          summary: "Standup",
          start: { date: "2026-09-15" },
          end: { date: "2026-09-16" },
          recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12"],
        },
        variant
      );
      expect(node).not.toBeNull();
    }
  });

  it("returns null when args are missing required fields", () => {
    expect(renderPreview(googleCalendarPreviews.create_event, { summary: "no start/end" }, "detailed")).toBeNull();
  });

  it("keeps the custom preview when nullable FastMCP arguments are explicitly null", () => {
    expect(
      renderPreview(
        googleCalendarPreviews.create_event,
        {
          summary: "Standup",
          start: { date: "2026-09-15" },
          end: { date: "2026-09-16" },
          reminders: null,
          attendees: null,
          recurrence: null,
        },
        "detailed"
      )
    ).not.toBeNull();
  });

  it("rejects unknown arguments instead of rendering a custom preview", () => {
    expect(
      renderPreview(
        googleCalendarPreviews.create_event,
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

  it("describes recurring creation distinctly and uses the standard RRULE humanizer", () => {
    const args = {
      summary: "Standup",
      start: { date: "2026-09-15" },
      end: { date: "2026-09-16" },
      recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12"],
    };
    expect(describeAction(googleCalendarPreviews.create_event, args)?.text).toContain("recurring");
    expect(humanizeRRule(args.recurrence[0])).toContain("Tuesday");
  });

  it("renders every Calendar read tool", () => {
    expect(renderPreview(googleCalendarPreviews.get_event, { event_id: "evt1" }, "compact")).not.toBeNull();
    expect(renderPreview(googleCalendarPreviews.list_events, { expand_recurring: true }, "detailed")).not.toBeNull();
    expect(
      renderPreview(googleCalendarPreviews.list_event_instances, { recurring_event_id: "series1" }, "compact")
    ).not.toBeNull();
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
