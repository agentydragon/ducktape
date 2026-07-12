import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry.tsx";
import { grocyResultPreviews } from "./responses.tsx";

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

  it("renders stock_entry_edit rows with the updated entry and changed fields", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(
          grocyResultPreviews.stock_entry_edit,
          [
            {
              kind: "ok",
              entry: {
                entry_id: 189,
                product_name: "Avocado Oil",
                amount: 500,
                qu_name: "Milliliter",
                location_name: "Pantry",
                best_before_date: "2027-01-15",
                open: true,
              },
              changes: { price: { old: 8.99, new: 9.99 }, open: { old: false, new: true } },
            },
          ],
          variant
        )
      ).not.toBeNull();
    }
  });

  it("renders Grocy read and shopping-list removal results", () => {
    const cases = [
      [
        grocyResultPreviews.stock_get,
        [
          {
            product_name: "Oats",
            amount: 500,
            amount_opened: 0,
            qu_name: "Gram",
            location_name: "Pantry",
            best_before_date: null,
          },
        ],
      ],
      [grocyResultPreviews.products_list, [{ id: 1, name: "Oats" }]],
      [grocyResultPreviews.quantity_units_list, [{ id: 2, name: "Gram", name_plural: "Grams" }]],
      [grocyResultPreviews.get_system_info, { grocy_version: "4.5.0" }],
      [
        grocyResultPreviews.shopping_list_get,
        {
          name: "Weekly",
          description: null,
          items: [{ item_id: 3, product_name: "Oats", amount: 2, qu_name: "Pack", note: null, done: false }],
        },
      ],
      [
        grocyResultPreviews.shopping_list_items_remove,
        [{ kind: "ok", item_id: 3, product_name: "Oats", amount: 2, qu_name: "Pack" }],
      ],
    ] as const;
    for (const [preview, result] of cases) expect(renderResultPreview(preview, result, "detailed")).not.toBeNull();
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
