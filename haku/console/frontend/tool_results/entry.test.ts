import { describe, expect, it } from "vitest";

import { unwrapToolResult } from "./entry.tsx";

describe("unwrapToolResult", () => {
  it("un-wraps FastMCP's {result: …} envelope when flagged and `result` is the sole key", () => {
    const rows = [{ kind: "ok", product_name: "Oats" }];
    expect(
      unwrapToolResult({
        content: [{ type: "text", text: JSON.stringify(rows) }],
        isError: false,
        structuredContent: { result: rows },
        _meta: { fastmcp: { wrap_result: true } },
      })
    ).toEqual(rows);
  });

  it("returns structuredContent as-is for a dict return (no wrap flag)", () => {
    const payload = { event_id: "evt1", html_link: "https://calendar.google.com/x" };
    expect(
      unwrapToolResult({ content: [{ type: "text", text: "{}" }], isError: false, structuredContent: payload })
    ).toEqual(payload);
  });

  it("keeps a genuine `result` key when the wrap flag is absent or other keys exist", () => {
    expect(unwrapToolResult({ isError: false, structuredContent: { result: [1] } })).toEqual({ result: [1] });
    expect(
      unwrapToolResult({
        isError: false,
        structuredContent: { result: [1], extra: 2 },
        _meta: { fastmcp: { wrap_result: true } },
      })
    ).toEqual({ result: [1], extra: 2 });
  });

  it("parses a single JSON text block when structuredContent is absent", () => {
    expect(unwrapToolResult({ content: [{ type: "text", text: '{"id": "d1"}' }], isError: false })).toEqual({
      id: "d1",
    });
  });

  it("returns null for prose text, multiple blocks, and empty content", () => {
    expect(unwrapToolResult({ content: [{ type: "text", text: "all done" }], isError: false })).toBeNull();
    expect(
      unwrapToolResult({
        content: [
          { type: "text", text: "{}" },
          { type: "text", text: "{}" },
        ],
        isError: false,
      })
    ).toBeNull();
    expect(unwrapToolResult({ content: [], isError: false })).toBeNull();
  });

  it("returns null for error results even when they carry structuredContent", () => {
    expect(
      unwrapToolResult({ content: [{ type: "text", text: "boom" }], isError: true, structuredContent: { x: 1 } })
    ).toBeNull();
  });

  it("returns null for a non-envelope value", () => {
    expect(unwrapToolResult("not a CallToolResult")).toBeNull();
    expect(unwrapToolResult(null)).toBeNull();
  });
});
