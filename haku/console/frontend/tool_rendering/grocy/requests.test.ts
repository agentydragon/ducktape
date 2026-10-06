import { describe, expect, it } from "vitest";

import { renderPreview } from "../entry";
import { grocyPreviews } from "./requests";

describe("grocyPreviews", () => {
  // `clear_fields` is a Python `set[StrEnum]`, which the generated JSON Schema carries as a
  // `uniqueItems` enum array. The preview-harness fixtures are only type-checked, never parsed, so
  // this is the one place the runtime validator sees that shape for each edit tool.
  it.each([
    {
      tool: "products_edit",
      preview: grocyPreviews.products_edit,
      args: {
        items: [{ product: "Oats", location: "Pantry", default_best_before_days: 270, clear_fields: ["description"] }],
      },
    },
    {
      tool: "stock_entry_edit",
      preview: grocyPreviews.stock_entry_edit,
      args: { items: [{ entry_id: 189, price: 9.99, location: "Pantry", open: true, clear_fields: ["note"] }] },
    },
    {
      tool: "shopping_list_item_edit",
      preview: grocyPreviews.shopping_list_item_edit,
      args: { item_id: 42, amount: 3, done: true, clear_fields: ["note"] },
    },
  ])("accepts clear_fields next to other edits on $tool", ({ preview, args }) => {
    expect(preview.schema.safeParse(args).error?.issues).toBeUndefined();
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
