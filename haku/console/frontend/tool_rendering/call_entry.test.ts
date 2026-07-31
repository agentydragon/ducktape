import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { z } from "zod";

import { defineCallPreview, renderCallPreview } from "./call_entry";

const zArgs = z.object({ subject: z.string() });
const zResult = z.object({ id: z.string() });
const preview = defineCallPreview(zArgs, zResult, ({ args }) => `widget:${args.subject}`);

// `render` wraps the widget in a `<Widget .../>` element (so its own hooks work), like
// entry.tsx's `renderPreview`/result_entry.tsx's `renderResultPreview` — so a dispatch test reads
// the element's props rather than the widget's return value.
function props(node: ReturnType<typeof renderCallPreview>): { args: unknown; result: unknown; variant: unknown } {
  return (node as ReactElement).props as { args: unknown; result: unknown; variant: unknown };
}

function toCallToolResult(structuredContent: unknown) {
  return { content: [{ type: "text", text: JSON.stringify(structuredContent) }], isError: false, structuredContent };
}

describe("renderCallPreview", () => {
  it("passes result: undefined to the widget when no result is given", () => {
    const node = renderCallPreview(preview, { subject: "hi" }, null, "compact");
    expect(node).not.toBeNull();
    expect(props(node)).toEqual({ args: { subject: "hi" }, result: undefined, variant: "compact" });
  });

  it("passes the parsed result to the widget once the result parses", () => {
    const node = renderCallPreview(preview, { subject: "hi" }, toCallToolResult({ id: "d1" }), "compact");
    expect(props(node)).toEqual({ args: { subject: "hi" }, result: { id: "d1" }, variant: "compact" });
  });

  it("falls back to result: undefined for an error result, a schema mismatch, or an unparseable envelope", () => {
    expect(
      props(
        renderCallPreview(
          preview,
          { subject: "hi" },
          { content: [{ type: "text", text: "boom" }], isError: true },
          "compact"
        )
      ).result
    ).toBeUndefined();
    expect(
      props(renderCallPreview(preview, { subject: "hi" }, toCallToolResult({ wrong: "shape" }), "compact")).result
    ).toBeUndefined();
    expect(
      props(renderCallPreview(preview, { subject: "hi" }, "not a CallToolResult", "compact")).result
    ).toBeUndefined();
  });

  it("returns null when the args themselves don't parse", () => {
    expect(renderCallPreview(preview, { subject: 5 }, null, "compact")).toBeNull();
  });
});
