import { describe, expect, it } from "vitest";

import { renderPreview } from "./entry.tsx";
import { grocyPreviews } from "./grocy.tsx";

describe("grocyPreviews", () => {
  it("renders products_create for valid args, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const node = renderPreview(
        grocyPreviews.products_create,
        { items: [{ name: "Oats", stock_qu: "Gram", location: "Pantry", default_best_before_days: 270 }] },
        variant
      );
      expect(node).not.toBeNull();
    }
  });

  it("renders products_edit for valid partial updates, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const node = renderPreview(
        grocyPreviews.products_edit,
        {
          items: [
            {
              product: "Oats",
              location: "Pantry",
              default_best_before_days: 270,
              clear_fields: ["description"],
            },
          ],
        },
        variant
      );
      expect(node).not.toBeNull();
    }
  });

  it("returns null (not false) when args don't match the tool's schema", () => {
    // Regression: the malformed shape a Jul 8 tool_request bug actually produced —
    // {entity_type, body} nesting instead of flat fields. `parsed.success && <X/>` used to
    // return `false` on a schema mismatch, not `null`, so the caller's `?? <pre>` raw-JSON
    // fallback never kicked in — the operator saw a blank Arguments field instead of the JSON.
    const node = renderPreview(
      grocyPreviews.products_create,
      { items: [{ entity_type: "products", body: { name: "Oats", stock_qu: "Gram" } }] },
      "detailed"
    );
    expect(node).toBeNull();
  });
});
