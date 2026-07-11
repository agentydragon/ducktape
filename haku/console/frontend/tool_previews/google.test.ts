import { describe, expect, it } from "vitest";

import { googleToolPreview } from "./google.tsx";

describe("googleToolPreview", () => {
  it("renders create_calendar_event for a valid all-day event", () => {
    const preview = googleToolPreview("create_calendar_event", {
      summary: "Standup",
      start: { date: "2026-09-15" },
      end: { date: "2026-09-16" },
    });
    expect(preview).not.toBeNull();
    expect(preview).not.toBe(false);
  });

  it("renders batch_modify_gmail_thread_labels for valid args", () => {
    expect(
      googleToolPreview("batch_modify_gmail_thread_labels", { thread_ids: ["t1"], add: ["urgent"], remove: [] })
    ).not.toBeNull();
  });

  it("renders create_gmail_draft for valid args", () => {
    expect(
      googleToolPreview("create_gmail_draft", { to: ["a@example.com"], subject: "Hello", body: "Hi" })
    ).not.toBeNull();
  });

  it("returns null when args don't match the tool's schema", () => {
    expect(googleToolPreview("create_calendar_event", { summary: "no start/end" })).toBeNull();
  });

  it("returns null for a tool it doesn't render", () => {
    expect(googleToolPreview("list_events", {})).toBeNull();
  });
});
