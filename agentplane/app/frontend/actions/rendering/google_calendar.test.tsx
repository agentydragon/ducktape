// @vitest-environment happy-dom

import { create } from "@bufbuild/protobuf";
import { describe, expect, it, vi } from "vitest";

import { ActionIdentitySchema } from "../../../../action_service/api/contracts/action_service_pb";
import { parseCallToolResult } from "../call_tool_result";
import { answerApi, mount } from "../testing/harness";
import { renderDetailsArguments, renderDetailsResult } from "./index";

const identity = (name: string) => create(ActionIdentitySchema, { group: "google_calendar", name });
const target = { event_id: "occurrence-1", calendar_id: "facilities@example.test" };
const link = "https://calendar.google.com/calendar/event?eid=occurrence-1";

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
          }
        : undefined
    );
    const action = identity("delete_event");
    const review = await mount(renderDetailsArguments(action, target, "request-1", true));
    await vi.waitFor(() => expect(review.querySelector("a")?.href).toBe(link));
    expect(review.textContent).toContain("Only this occurrence");
    expect(review.textContent).not.toContain(target.event_id);
    expect(sent).toHaveLength(1);
    expect(sent[0]?.query.size).toBe(0);

    const receipt = await mount(renderDetailsArguments(action, target, "request-1"));
    expect(receipt.textContent).toContain(target.event_id);
    expect(receipt.textContent).not.toContain("Facilities coordination");
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
