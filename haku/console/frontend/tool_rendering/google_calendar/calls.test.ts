import { describe, expect, it } from "vitest";

import { toolActionDescription } from "../actions";
import { renderCallPreview } from "../call_entry";
import { GOOGLE_CALENDAR_SERVER_ID } from "../server_ids";
import { googleCalendarCallPreviews } from "./calls";

const RECURRING_ARGS = {
  summary: "Standup",
  start: { date: "2026-09-15" },
  end: { date: "2026-09-16" },
  recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12"],
};

describe("googleCalendarCallPreviews.create_event", () => {
  it("renders the pending (arguments) view before the call has executed, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderCallPreview(googleCalendarCallPreviews.create_event, RECURRING_ARGS, null, variant)).not.toBeNull();
    }
  });

  it("returns null when the arguments don't parse", () => {
    expect(
      renderCallPreview(googleCalendarCallPreviews.create_event, { summary: "no start/end" }, null, "compact")
    ).toBeNull();
  });

  it("describes recurring creation distinctly from a one-off event", () => {
    expect(toolActionDescription(GOOGLE_CALENDAR_SERVER_ID, "create_event", RECURRING_ARGS)?.text).toContain(
      "recurring"
    );
    expect(
      toolActionDescription(GOOGLE_CALENDAR_SERVER_ID, "create_event", { ...RECURRING_ARGS, recurrence: undefined })
        ?.text
    ).not.toContain("recurring");
  });
});

const UPDATE_ARGS = { event_id: "evt1", summary: "Court hearing" };

describe("googleCalendarCallPreviews.update_event", () => {
  it("renders the pending (patch) view before the call has executed, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderCallPreview(googleCalendarCallPreviews.update_event, UPDATE_ARGS, null, variant)).not.toBeNull();
    }
  });

  it("returns null when the arguments don't parse", () => {
    expect(
      renderCallPreview(googleCalendarCallPreviews.update_event, { summary: "no event_id" }, null, "compact")
    ).toBeNull();
  });

  it("describes recurring vs. non-recurring patches distinctly", () => {
    expect(
      toolActionDescription(GOOGLE_CALENDAR_SERVER_ID, "update_event", {
        ...UPDATE_ARGS,
        recurrence: ["RRULE:FREQ=WEEKLY"],
      })?.text
    ).toContain("recurring");
    expect(toolActionDescription(GOOGLE_CALENDAR_SERVER_ID, "update_event", UPDATE_ARGS)?.text).not.toContain(
      "recurring"
    );
  });
});
