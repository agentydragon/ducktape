import { describe, expect, it } from "vitest";

import { renderPreview } from "../entry.tsx";
import { grocyPreviews } from "./requests.tsx";

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

  it("renders shopping_list_get in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderPreview(grocyPreviews.shopping_list_get, { shopping_list: "Weekly" }, variant)).not.toBeNull();
    }
  });

  it("renders shopping_list_items_add with product and note-only items, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const node = renderPreview(
        grocyPreviews.shopping_list_items_add,
        {
          items: [
            { shopping_list: "Weekly", product: "Oats", amount: 2 },
            { shopping_list: "Weekly", note: "paper towels?" },
          ],
        },
        variant
      );
      expect(node).not.toBeNull();
    }
  });

  it("renders shopping_list_item_edit in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const node = renderPreview(
        grocyPreviews.shopping_list_item_edit,
        { item_id: 42, amount: 3, done: true, clear_fields: ["note"] },
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
