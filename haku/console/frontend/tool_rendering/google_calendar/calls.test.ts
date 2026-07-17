import { describe, expect, it } from "vitest";

import { describeCallAction, renderCallPreview } from "../call_entry.tsx";
import { googleCalendarCallPreviews } from "./calls.tsx";

const RECURRING_ARGS = {
  summary: "Standup",
  start: { date: "2026-09-15" },
  end: { date: "2026-09-16" },
  recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12"],
};

const RESULT = {
  event_id: "evt1",
  status: "confirmed",
  summary: "Standup",
  start: { date: "2026-09-15" },
  end: { date: "2026-09-16" },
  recurrence: RECURRING_ARGS.recurrence,
  html_link: "https://www.google.com/calendar/event?eid=evt1",
};

function toCallToolResult(structuredContent: unknown) {
  return { content: [{ type: "text", text: JSON.stringify(structuredContent) }], isError: false, structuredContent };
}

describe("googleCalendarCallPreviews.create_event", () => {
  it("renders the pending (arguments) view before the call has executed, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderCallPreview(googleCalendarCallPreviews.create_event, RECURRING_ARGS, null, variant)).not.toBeNull();
    }
  });

  it("renders the finished (event) view once the event has been created, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderCallPreview(googleCalendarCallPreviews.create_event, RECURRING_ARGS, toCallToolResult(RESULT), variant)
      ).not.toBeNull();
    }
  });

  it("falls back to the pending view when there is no successful result yet", () => {
    expect(
      renderCallPreview(
        googleCalendarCallPreviews.create_event,
        RECURRING_ARGS,
        { content: [{ type: "text", text: "boom" }], isError: true },
        "compact"
      )
    ).not.toBeNull();
  });

  it("returns null when the arguments don't parse", () => {
    expect(
      renderCallPreview(googleCalendarCallPreviews.create_event, { summary: "no start/end" }, null, "compact")
    ).toBeNull();
  });

  it("describes recurring creation distinctly from a one-off event", () => {
    expect(describeCallAction(googleCalendarCallPreviews.create_event, RECURRING_ARGS)?.text).toContain("recurring");
    expect(
      describeCallAction(googleCalendarCallPreviews.create_event, { ...RECURRING_ARGS, recurrence: undefined })?.text
    ).not.toContain("recurring");
  });
});
