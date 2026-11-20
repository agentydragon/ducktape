// @vitest-environment happy-dom

import { create } from "@bufbuild/protobuf";
import { describe, expect, it, vi } from "vitest";

import { ActionIdentitySchema } from "../../../../action_service/api/contracts/action_service_pb";
import { parseCallToolResult } from "../call_tool_result";
import { answerApi, mount } from "../testing/harness";
import { renderActionCall, renderDetailsArguments, renderDetailsResult } from "./index";

const identity = (name: string) => create(ActionIdentitySchema, { group: "google_calendar", name });
const target = { event_id: "occurrence-1", calendar_id: "facilities@example.test" };
const link = "https://calendar.google.com/calendar/event?eid=occurrence-1";
const calendarLink = "https://calendar.google.com/calendar?cid=ZmFjaWxpdGllc0BleGFtcGxlLnRlc3Q";

function success(value: unknown) {
  const envelope = parseCallToolResult({
    structuredContent: value,
    content: [{ type: "text", text: JSON.stringify(value) }],
  });
  if (!envelope) throw new Error("invalid test envelope");
  return envelope;
}

describe("Calendar Action review", () => {
  it("shows only supplied changes, distinguishes clears, and preserves the requested event timezone", async () => {
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
    expect(review.textContent).toContain("9:00 AM");
    expect(review.textContent).toContain("America/Los_Angeles");
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
    const wrapped = parseCallToolResult({
      content: [],
      structuredContent: { result: null },
      _meta: { fastmcp: { wrap_result: true } },
    });
    if (!wrapped) throw new Error("invalid delete envelope");
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
