// @vitest-environment happy-dom

import { create } from "@bufbuild/protobuf";
import { describe, expect, it, vi } from "vitest";

import { ActionIdentitySchema } from "../../../../action_service/api/contracts/action_service_pb";
import { answerApi, mount, toolResult } from "../testing/harness";
import { eventTime, formatEventRange, formatEventTime } from "./google_calendar_time";
import {
  actionOffersCardDecisionButtons,
  renderActionCall,
  renderDetailsArguments,
  renderDetailsResult,
  renderPaneCollapsed,
  renderPaneOpened,
} from "./index";

const identity = (name: string) => create(ActionIdentitySchema, { group: "google_calendar", name });
const target = { event_id: "occurrence-1", calendar_id: "facilities@example.test" };
const link = "https://calendar.google.com/calendar/event?eid=occurrence-1";
const calendarLink = "https://calendar.google.com/calendar?cid=ZmFjaWxpdGllc0BleGFtcGxlLnRlc3Q";

function success(value: unknown) {
  return toolResult({ structuredContent: value, content: [{ type: "text", text: JSON.stringify(value) }] });
}

describe("Calendar Action review", () => {
  it("offers compact list decisions with the source and broadening options visible and the dates in the opened view", async () => {
    const action = identity("list_events");
    const args = {
      calendar_id: target.calendar_id,
      time_min: "2026-11-24T00:00:00-08:00",
      time_max: "2026-12-01T00:00:00-08:00",
      expand_recurring: true,
      max_results: 120,
    };
    answerApi(({ path }) =>
      path.endsWith("/request-1/google-calendar-event-preview")
        ? { calendar: { calendarId: target.calendar_id, summary: "Facilities calendar" } }
        : undefined
    );
    const collapsed = await mount(renderPaneCollapsed(action, args, "request-1"));
    expect(collapsed.textContent).toContain(target.calendar_id);
    await vi.waitFor(() => expect(collapsed.textContent).toContain("Facilities calendar"));
    expect(collapsed.textContent).toContain(target.calendar_id);
    expect(collapsed.textContent).toContain("Expand recurring events into occurrences");
    expect(collapsed.textContent).not.toContain("Results per page");
    expect(collapsed.textContent).not.toContain("Nov 24");
    expect(collapsed.textContent).not.toContain("Dec 1");
    expect(actionOffersCardDecisionButtons(action, args)).toBe(true);
    expect(actionOffersCardDecisionButtons(action, {})).toBe(true);

    const opened = await mount(renderPaneOpened(action, args, "request-1"));
    expect(opened.textContent).toContain("Results per page · 120");
    expect(opened.textContent).toContain("Ending after · Tue, Nov 24, 2026");
    expect(opened.textContent).toContain("Starting before · Tue, Dec 1, 2026");
    await vi.waitFor(() => expect(opened.querySelector("a")?.href).toBe(calendarLink));

    for (const unreviewable of [
      { ...args, include_deleted: true },
      { ...args, account: "other-account@example.test" },
      { ...args, max_results: 1000 },
      { ...args, expand_recurring: "yes" },
      { ...args, time_min: "not a date bound" },
    ]) {
      expect(actionOffersCardDecisionButtons(action, unreviewable)).toBe(false);
    }
    const unknown = await mount(renderPaneOpened(action, { ...args, include_deleted: true }));
    expect(unknown.textContent).toContain("include_deleted");
  });

  it.each([
    {
      name: "displays a single range in the viewer's zone",
      start: { date_time: "2026-11-23T17:00:00Z", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-11-23T17:30:00Z", time_zone: "America/Los_Angeles" },
      range: "9:00 AM – 9:30 AM",
    },
    {
      name: "shows both offsets when a clock change repeats the local time",
      start: { date_time: "2026-11-01T08:15:00Z", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-11-01T09:15:00Z", time_zone: "America/Los_Angeles" },
      range: "1:15 AM GMT-7 – 1:15 AM GMT-8",
    },
    {
      name: "converts both endpoints when their submitted zones differ",
      start: { date_time: "2026-11-23T17:00:00Z", time_zone: "America/Los_Angeles" },
      end: { date_time: "2026-11-23T19:00:00Z", time_zone: "America/New_York" },
      range: "9:00 AM – 11:00 AM",
    },
  ])("$name", ({ start, end, range }) => {
    expect(formatEventRange(eventTime.parse(start), eventTime.parse(end), "America/Los_Angeles")).toContain(range);
  });

  it.each([
    [{ date: "2026-11-23" }, "Mon, Nov 23, 2026"],
    [{ date_time: "2026-11-23T09:00:00", time_zone: "America/Los_Angeles" }, "5:00 PM"],
    [{ date_time: "2026-11-01T01:15:00", time_zone: "America/Los_Angeles" }, "1:15 AM · America/Los_Angeles"],
    [{ date_time: "2026-03-08T02:15:00", time_zone: "America/Los_Angeles" }, "2:15 AM · America/Los_Angeles"],
    [{ date_time: "2026-11-23T09:00:00", time_zone: "Unrecognized/Zone" }, "9:00 AM · Unrecognized/Zone"],
    [{ date_time: "2026-11-23T09:00:00" }, "9:00 AM · timezone unspecified"],
  ] as const)("preserves all-day and unresolved wall times for %o", (submitted, visible) => {
    expect(formatEventTime(eventTime.parse(submitted), "UTC")).toContain(visible);
  });

  it("separates the existing event schedule from changes to its time, location, and attendees", async () => {
    const scheduled = { dateTime: "2026-11-23T17:00:00Z", timeZone: "America/Los_Angeles" };
    answerApi(({ path }) =>
      path.endsWith("/request-1/google-calendar-event-preview")
        ? {
            event: {
              eventId: target.event_id,
              summary: "Facilities coordination",
              htmlLink: link,
              start: scheduled,
              end: { ...scheduled, dateTime: "2026-11-23T17:30:00Z" },
              scope: "GOOGLE_CALENDAR_EVENT_SCOPE_OCCURRENCE",
            },
          }
        : undefined
    );
    const card = await mount(
      renderPaneCollapsed(
        identity("update_event"),
        {
          ...target,
          start: { date_time: "2026-11-23T18:00:00Z", time_zone: "America/Los_Angeles" },
          location: "Test conference room",
          attendees: ["attendee@example.test"],
        },
        "request-1"
      )
    );
    await vi.waitFor(() => expect(card.textContent).toContain("Facilities coordination"));
    expect(card.textContent).toContain("Changes: time, location, attendees");
    expect(card.textContent).not.toContain("Test conference room");
    expect(card.textContent).not.toContain("attendee@example.test");
    const review = await mount(
      renderDetailsArguments(
        identity("update_event"),
        {
          ...target,
          start: { date_time: "2026-11-23T18:00:00Z", time_zone: "America/Los_Angeles" },
          location: "Test conference room",
          attendees: ["attendee@example.test"],
        },
        "request-1",
        true
      )
    );
    expect(review.textContent).toContain("Change location to Test conference room");
    expect(review.textContent).toContain("Replace attendees with attendee@example.test");
    expect(card.querySelector("a")).toBeNull();
  });

  it("keeps complex creation details and list pagination in the opened review", async () => {
    const create = identity("create_event");
    const args = {
      summary: "Planning",
      start: { date: "2026-11-26" },
      end: { date: "2026-11-27" },
      attendees: ["guest@example.test"],
      location: "Conference room",
    };
    const collapsed = await mount(renderPaneCollapsed(create, args));
    const review = await mount(renderDetailsArguments(create, args));
    expect(collapsed.textContent).toContain("Planning");
    expect(collapsed.textContent).not.toContain("guest@example.test");
    expect(review.textContent).toContain("guest@example.test");
    expect(review.textContent).toContain("Conference room");
    expect(actionOffersCardDecisionButtons(create, args)).toBe(false);

    const list = identity("list_events");
    const page = { max_results: 5, page_token: "next" };
    expect((await mount(renderPaneCollapsed(list, page))).textContent).not.toContain("Results per page");
    expect((await mount(renderDetailsArguments(list, page))).textContent).toContain("Results per page · 5");
  });

  it("shows only supplied changes and distinguishes clears", async () => {
    const review = await mount(
      renderDetailsArguments(identity("update_event"), {
        ...target,
        description: null,
        location: "",
        attendees: [],
        reminders: [],
        start: { date_time: "2026-11-23T17:00:00Z", time_zone: "America/Los_Angeles" },
      })
    );
    expect(review.textContent).toContain("Clear location");
    expect(review.textContent).toContain("Remove all attendees");
    expect(review.textContent).toContain("Use calendar defaults");
    expect(review.textContent).toContain("New time · Starts");
    expect(review.textContent).not.toContain("Description");
    expect(review.textContent).not.toContain("End");
  });

  it("shows a bounded pending lookup, identifies occurrence scope, and keeps history grounded in the request", async () => {
    const sent = answerApi(({ path }) =>
      path.endsWith("/request-1/google-calendar-event-preview")
        ? {
            event: {
              eventId: target.event_id,
              summary: "Facilities coordination",
              htmlLink: link,
              scope: "GOOGLE_CALENDAR_EVENT_SCOPE_OCCURRENCE",
            },
            calendar: { calendarId: target.calendar_id, summary: "Facilities calendar" },
          }
        : undefined
    );
    const action = identity("delete_event");
    const review = await mount(renderDetailsArguments(action, target, "request-1", true));
    await vi.waitFor(() =>
      expect([...review.querySelectorAll("a")].map((anchor) => anchor.href)).toEqual([link, calendarLink])
    );
    expect(review.textContent).toContain("Only this occurrence");
    expect(review.textContent).not.toContain(target.event_id);
    expect(review.textContent).not.toContain(target.calendar_id);
    expect(sent).toHaveLength(1);
    expect(sent[0]?.query.size).toBe(0);

    const receipt = await mount(renderDetailsArguments(action, target, "request-1"));
    expect(receipt.textContent).toContain(target.event_id);
    expect(receipt.textContent).toContain(target.calendar_id);
    expect(receipt.textContent).not.toContain("Facilities coordination");
    expect(sent).toHaveLength(1);
  });

  it.each([
    ["create_event", { summary: "Office closure", start: { date: "2026-11-26" }, end: { date: "2026-11-27" } }],
    ["list_events", { query: "Office closure" }],
  ] as const)("resolves the explicit pending calendar without an event ID for %s", async (name, fields) => {
    const args = { ...fields, calendar_id: target.calendar_id };
    const sent = answerApi(({ path }) =>
      path.endsWith("/request-1/google-calendar-event-preview")
        ? { calendar: { calendarId: target.calendar_id, summary: "Facilities calendar" } }
        : undefined
    );
    const review = await mount(renderDetailsArguments(identity(name), args, "request-1", true));
    await vi.waitFor(() => expect(review.querySelector("a")?.href).toBe(calendarLink));
    expect(review.textContent).toContain("Calendar · Facilities calendar");
    expect(review.textContent).not.toContain(target.calendar_id);

    const receipt = await mount(renderDetailsArguments(identity(name), args, "request-1"));
    expect(receipt.textContent).toContain(target.calendar_id);
    expect(receipt.querySelector("a")).toBeNull();
    expect(sent).toHaveLength(1);
  });

  it("keeps the primary alias and does not use a summary for another calendar", async () => {
    const sent = answerApi(() => ({
      event: { eventId: target.event_id, summary: "Office closure", htmlLink: link },
      calendar: { calendarId: "other@example.test", summary: "Unrelated calendar" },
    }));
    const primary = await mount(renderDetailsArguments(identity("list_events"), {}, "primary-request", true));
    expect(primary.textContent).toContain("Primary calendar");
    expect(primary.querySelector("a")).toBeNull();

    const review = await mount(renderDetailsArguments(identity("get_event"), target, "request-1", true));
    await vi.waitFor(() => expect(review.querySelector("a")?.href).toBe(link));
    expect(review.textContent).toContain(target.calendar_id);
    expect(review.textContent).not.toContain("Unrelated calendar");
    expect(review.querySelectorAll("a")).toHaveLength(1);
    expect(sent).toHaveLength(1);
  });

  it("retains the IDs and unresolved scope when lookup fails", async () => {
    const sent = answerApi(() => Response.json({}, { status: 404 }));
    const view = await mount(renderDetailsArguments(identity("delete_event"), target, "missing", true));
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(view.textContent).toContain(target.event_id);
    expect(view.textContent).toContain(target.calendar_id);
    expect(view.textContent).toContain("whether this affects one event, an occurrence, or a series");
    expect(view.querySelector("a")).toBeNull();
  });

  it("keeps unrecognized mutation arguments available as recorded data", async () => {
    const view = await mount(renderDetailsArguments(identity("update_event"), { ...target, send_updates: "all" }));
    expect(view.textContent).toContain("Details shown as recorded data");
    expect(view.textContent).toContain('"send_updates": "all"');
  });
});

describe("Calendar Action results", () => {
  it("keeps the ordinary argument view available before an event result exists", () => {
    const action = identity("get_event");
    expect(renderActionCall(action, target, undefined, "pending")).toEqual({ content: null, includesResult: false });
  });

  it("keeps the requested event ID when the result names a different event", async () => {
    const call = renderActionCall(
      identity("get_event"),
      target,
      success({ event_id: "different-event", summary: "A different event" }),
      "completed"
    );
    expect(call?.includesResult).toBe(true);
    const view = await mount(call?.content);
    expect(view.textContent).toContain(target.event_id);
    expect(view.textContent).toContain("A different event");
  });

  it("keeps the series ID and only replaces the calendar ID when an instance page names it", async () => {
    const action = identity("list_event_instances");
    const args = { calendar_id: target.calendar_id, recurring_event_id: "series-1" };
    const page = { events: [], summary: "Recorded Facilities calendar" };
    const call = renderActionCall(action, args, success(page), "completed");
    expect(call?.includesResult).toBe(true);
    const view = await mount(call?.content);
    expect(view.querySelector("a")?.href).toBe(calendarLink);
    expect(view.textContent).toContain("series-1");
    expect(view.textContent).not.toContain(target.calendar_id);
    expect(renderActionCall(action, args, success({ events: [] }), "completed")).toBeUndefined();
  });

  it("links a recorded calendar title using the exact non-primary ID in the saved list arguments", async () => {
    const action = identity("list_events");
    const result = success({ summary: "Recorded Facilities calendar", events: [] });
    const sent = answerApi(() => {
      throw new Error("Calendar receipt should not look up data");
    });
    const receipt = await mount(
      renderDetailsResult(action, result, { requestId: "completed", arguments: { calendar_id: target.calendar_id } })
    );
    expect(receipt.querySelector("a")?.textContent).toBe("Recorded Facilities calendar");
    expect(receipt.querySelector("a")?.href).toBe(calendarLink);

    const primary = await mount(
      renderDetailsResult(action, result, { requestId: "completed", arguments: { calendar_id: "primary" } })
    );
    expect(primary.textContent).toContain("Recorded Facilities calendar");
    expect(primary.querySelector("a")).toBeNull();

    const longId = "設備".repeat(180);
    const oversized = await mount(
      renderDetailsResult(action, result, { requestId: "completed", arguments: { calendar_id: longId } })
    );
    expect(oversized.textContent).toContain("Recorded Facilities calendar");
    expect(oversized.querySelector("a")).toBeNull();
    expect(sent).toHaveLength(0);
  });

  it("renders linked all-day events and cancelled occurrences without leaking page tokens", async () => {
    const agenda = await mount(
      renderDetailsResult(
        identity("list_events"),
        success({
          summary: "Facilities calendar",
          next_page_token: "opaque-cursor",
          events: [
            {
              ...target,
              summary: "Office closure",
              html_link: link,
              start: { date: "2026-11-26" },
              end: { date: "2026-11-28" },
            },
            { event_id: "cancelled-2", status: "cancelled", original_start_time: { date: "2026-11-30" } },
          ],
        })
      )
    );
    expect(agenda.querySelector("a")?.href).toBe(link);
    expect(agenda.textContent).toContain("All day · Thu, Nov 26, 2026 – Fri, Nov 27, 2026");
    expect(agenda.textContent).toContain("Cancelled");
    expect(agenda.textContent).toContain("Original occurrence · Mon, Nov 30, 2026");
    expect(agenda.textContent).toContain("More events are available");
    expect(agenda.textContent).not.toContain("opaque-cursor");
  });

  it("accepts an empty agenda and keeps unexpected or distinct outputs on the generic path", async () => {
    const action = identity("list_event_instances");
    const blank = await mount(renderDetailsResult(action, success({ events: [] })));
    expect(blank.textContent).toContain("No events on this page");
    expect(renderDetailsResult(action, success({ items: [] }))).toBeUndefined();
    const valid = success({ events: [] });
    expect(
      renderDetailsResult(action, {
        ...valid,
        content: [...valid.content, { type: "text", text: "Provider note" }],
      })
    ).toBeUndefined();
  });

  it("renders a title as plain text if the provider link is outside Google Calendar", async () => {
    const result = await mount(
      renderDetailsResult(
        identity("get_event"),
        success({
          event_id: "evt1",
          summary: "Event title",
          html_link: "https://example.test/calendar/event?eid=evt1",
        })
      )
    );
    expect(result.textContent).toContain("Event title");
    expect(result.querySelector("a")).toBeNull();
  });

  it("confirms only a successful empty delete result", async () => {
    const action = identity("delete_event");
    const wrapped = toolResult({
      content: [],
      structuredContent: { result: null },
      _meta: { fastmcp: { wrap_result: true } },
    });
    const result = await mount(renderDetailsResult(action, wrapped));
    expect(result.textContent).toContain("Deletion confirmed.");
    expect(renderDetailsResult(action, { content: [], isError: true })).toBeUndefined();
    expect(renderDetailsResult(action, success({ unexpected: true }))).toBeUndefined();
    expect(
      renderDetailsResult(action, {
        content: [{ type: "text", text: "Provider message" }],
        isError: false,
      })
    ).toBeUndefined();
  });
});
