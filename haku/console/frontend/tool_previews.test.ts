import { describe, expect, it } from "vitest";

import { toolPreview } from "./tool_previews.tsx";

describe("toolPreview", () => {
  it("renders a preview for valid grocy-sf products_create args", () => {
    const preview = toolPreview("grocy-sf", "products_create", {
      items: [{ name: "Oats", stock_qu: "Gram", location: "Pantry", default_best_before_days: 270 }],
    });
    expect(preview).not.toBeNull();
    expect(preview).not.toBe(false);
  });

  it("falls through to null (not false) when args don't match the tool's schema", () => {
    // Regression: the malformed shape a Jul 8 tool_request bug actually produced —
    // {entity_type, body} nesting instead of flat fields. `parsed.success && <X/>`
    // used to return `false` on a schema mismatch, not `null`, so `toolPreview`'s
    // `??` chain (and console_panel.tsx's `nice ?? <pre>...</pre>` raw-JSON
    // fallback) never kicked in — the operator saw a blank Arguments field
    // instead of the raw JSON.
    const preview = toolPreview("grocy-sf", "products_create", {
      items: [{ entity_type: "products", body: { name: "Oats", stock_qu: "Gram" } }],
    });
    expect(preview).toBeNull();
  });

  it("returns null for an unknown server/tool combination", () => {
    expect(toolPreview("some-other-server", "some_tool", {})).toBeNull();
  });
});
