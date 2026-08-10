import { describe, expect, it } from "vitest";

import type { ToolCallPage, ToolCallRecord } from "./client";
import { appendPage, mergeNewestPage } from "./tool_calls_page";

function record(id: string): ToolCallRecord {
  return {
    tool_call_id: id,
    server_id: "smoke",
    tool_name: "echo",
    caller: { kind: "operator" },
    status: "ok",
    created_at: "2026-07-20T12:00:00Z",
    updated_at: "2026-07-20T12:00:00Z",
    arguments: {},
    rationale: "",
  };
}

function page(ids: string[], nextCursor: string | null): ToolCallPage {
  return { records: ids.map(record), nextCursor };
}

const ids = (result: ToolCallPage) => result.records.map((r) => r.tool_call_id);

describe("mergeNewestPage", () => {
  it("keeps the pages already scrolled back through under a refreshed first page", () => {
    const loaded = page(["c", "b", "a"], "cursor-a");
    const merged = mergeNewestPage(page(["d", "c"], "cursor-c"), loaded);
    expect(ids(merged)).toEqual(["d", "c", "b", "a"]);
    // The deepest page loaded owns the resume position; the first page knows nothing below itself.
    expect(merged.nextCursor).toBe("cursor-a");
  });

  it("replaces the list when the refreshed page shares no call with it", () => {
    // More than a page arrived since the last read, so the calls between them were never fetched.
    const merged = mergeNewestPage(page(["z", "y"], "cursor-y"), page(["c", "b"], "cursor-b"));
    expect(ids(merged)).toEqual(["z", "y"]);
    expect(merged.nextCursor).toBe("cursor-y");
  });

  it("takes the page as-is on the first load", () => {
    expect(mergeNewestPage(page(["b", "a"], null), null)).toEqual(page(["b", "a"], null));
  });
});

describe("appendPage", () => {
  it("appends older calls and advances the resume position", () => {
    const appended = appendPage(page(["b", "a"], null), page(["d", "c"], "cursor-c"));
    expect(ids(appended)).toEqual(["d", "c", "b", "a"]);
    expect(appended.nextCursor).toBeNull();
  });

  it("drops a call the next page repeats", () => {
    // A submission between the two requests shifts the ledger under the cursor.
    const appended = appendPage(page(["c", "b"], "cursor-b"), page(["d", "c"], "cursor-c"));
    expect(ids(appended)).toEqual(["d", "c", "b"]);
  });
});
