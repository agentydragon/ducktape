import { describe, expect, it } from "vitest";

import { renderResultPreview } from "./entry.tsx";
import { grocyResultPreviews } from "./grocy.tsx";

const STOCK_ADD_OK_ROW = {
  kind: "ok",
  product_name: "Avocado Oil",
  transaction_id: "6f0b2c",
  amount_delta: 500.0,
  new_amount: 500.0,
  qu_name: "Milliliter",
  stock_qu_name: null,
  location_name: "Pantry",
  entry_id: 189,
  best_before_date: "2027-01-15",
};

describe("grocyResultPreviews", () => {
  it("renders stock_add rows (ok + failed mixed), in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(
          grocyResultPreviews.stock_add,
          [STOCK_ADD_OK_ROW, { kind: "error", error: "No product 'Avocado Oli' found" }],
          variant
        )
      ).not.toBeNull();
    }
  });

  it("renders products_create rows with only created_object_id (grocy_mcp's CreateOk today)", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(
          grocyResultPreviews.products_create,
          [
            { kind: "ok", created_object_id: 123 },
            { kind: "error", error: "location not found" },
          ],
          variant
        )
      ).not.toBeNull();
    }
  });

  it("renders shopping_list_items_add rows, including note-only items (null product/qu)", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(
          grocyResultPreviews.shopping_list_items_add,
          [
            { kind: "ok", item_id: 55, product_name: "Oat milk", amount: 6, qu_name: "Carton" },
            { kind: "ok", item_id: 56, product_name: null, amount: 1, qu_name: null },
          ],
          variant
        )
      ).not.toBeNull();
    }
  });

  it("returns null for a malformed ok row instead of rendering it as a failure", () => {
    // kind "ok" without the ok-row fields must fail the whole parse (→ raw JSON fallback),
    // not fall through to the failed-row branch and paint a success red.
    expect(renderResultPreview(grocyResultPreviews.stock_add, [{ kind: "ok" }], "detailed")).toBeNull();
  });

  it("returns null when the payload is not a list (e.g. the un-unwrapped envelope)", () => {
    expect(renderResultPreview(grocyResultPreviews.stock_add, { result: [STOCK_ADD_OK_ROW] }, "compact")).toBeNull();
  });
});
