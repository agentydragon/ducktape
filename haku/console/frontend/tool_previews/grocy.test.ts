import { describe, expect, it } from "vitest";

import { grocyToolPreview } from "./grocy.tsx";

describe("grocyToolPreview", () => {
  it("renders products_create for valid args", () => {
    const preview = grocyToolPreview("products_create", {
      items: [{ name: "Oats", stock_qu: "Gram", location: "Pantry", default_best_before_days: 270 }],
    });
    expect(preview).not.toBeNull();
    expect(preview).not.toBe(false);
  });

  it("falls through to null (not false) when args don't match the tool's schema", () => {
    // Regression: the malformed shape a Jul 8 tool_request bug actually produced —
    // {entity_type, body} nesting instead of flat fields. `parsed.success && <X/>` used to
    // return `false` on a schema mismatch, not `null`, so the caller's `?? <pre>` raw-JSON
    // fallback never kicked in — the operator saw a blank Arguments field instead of the JSON.
    const preview = grocyToolPreview("products_create", {
      items: [{ entity_type: "products", body: { name: "Oats", stock_qu: "Gram" } }],
    });
    expect(preview).toBeNull();
  });
});
