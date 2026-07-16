import { z } from "zod";

import { callOperatorMcpTool } from "./mcp_client.ts";

const zReferenceItem = z.object({ id: z.coerce.number().int(), name: z.string() });
const nullableReferenceId = z.preprocess(
  (value) => (value === null || value === "" || value === 0 || value === "0" ? null : value),
  z.coerce.number().int().nullable()
);
const nullableNumber = z.preprocess(
  (value) => (value === null || value === "" ? null : value),
  z.coerce.number().nullable()
);
const nullableString = z.preprocess((value) => (value === null || value === "" ? null : value), z.string().nullable());
const zProduct = z.object({
  id: z.coerce.number().int(),
  name: z.string(),
  location_id: z.coerce.number().int(),
  qu_id_stock: z.coerce.number().int(),
  qu_id_purchase: z.coerce.number().int(),
  qu_id_consume: z.coerce.number().int(),
  min_stock_amount: z.coerce.number(),
  default_best_before_days: z.coerce.number().int(),
  due_type: z.coerce.number().int(),
  parent_product_id: nullableReferenceId.default(null),
  product_group_id: nullableReferenceId.default(null),
  description: nullableString.default(null),
  calories: nullableNumber.default(null),
});
const zBooleanish = z
  .union([z.boolean(), z.literal(0), z.literal(1), z.literal("0"), z.literal("1")])
  .transform((value) => value === true || value === 1 || value === "1");
const zShoppingListItem = z.object({
  item_id: z.coerce.number().int(),
  product_name: nullableString.default(null),
  note: nullableString.default(null),
  amount: z.coerce.number(),
  qu_name: nullableString.default(null),
  done: zBooleanish,
});
const zShoppingList = z.object({ items: z.array(zShoppingListItem) });

// Frontend-owned projection of the ordinary Grocy read results needed to resolve IDs and render
// old-to-new diffs. No MCP tool returns this aggregate; loadGrocyReferenceData composes it locally.
const zGrocyReferenceData = z.object({
  products: z.array(zProduct),
  locations: z.array(zReferenceItem),
  quantity_units: z.array(zReferenceItem),
  product_groups: z.array(zReferenceItem),
  shopping_lists: z.array(zReferenceItem),
  shopping_list_items: z.array(zShoppingListItem),
});

export type GrocyReferenceData = z.infer<typeof zGrocyReferenceData>;

let cachedReferenceData: Promise<GrocyReferenceData> | null = null;

async function loadGrocyReferenceData(): Promise<GrocyReferenceData> {
  const [productsPayload, locationsPayload, unitsPayload, groupsPayload, listsPayload] = await Promise.all([
    callOperatorMcpTool("grocy_sf_products_list", { detail: "full" }),
    callOperatorMcpTool("grocy_sf_locations_list", {}),
    callOperatorMcpTool("grocy_sf_quantity_units_list", {}),
    callOperatorMcpTool("grocy_sf_product_groups_list", {}),
    callOperatorMcpTool("grocy_sf_shopping_lists_list", {}),
  ]);
  const shoppingLists = z.array(zReferenceItem).parse(listsPayload);
  const listPayloads = await Promise.all(
    shoppingLists.map((list) => callOperatorMcpTool("grocy_sf_shopping_list_get", { shopping_list: list.id }))
  );
  return zGrocyReferenceData.parse({
    products: productsPayload,
    locations: locationsPayload,
    quantity_units: unitsPayload,
    product_groups: groupsPayload,
    shopping_lists: shoppingLists,
    shopping_list_items: listPayloads.flatMap((payload) => zShoppingList.parse(payload).items),
  });
}

// Compose the remote server's own read tools in the browser. All calls use the linked Operator
// credential through `/mcp`; the shared promise prevents each rendered widget from refetching the
// same reference tables.
export async function fetchGrocyReferenceData(): Promise<GrocyReferenceData> {
  cachedReferenceData ??= loadGrocyReferenceData().catch((error: unknown) => {
    cachedReferenceData = null;
    throw error;
  });
  return cachedReferenceData;
}
