import type { GrocyReferenceData } from "../../grocy_client";

// Grocy reference rows used by this server's id-to-name and old-to-new preview fixtures.
export const GROCY_REFERENCE_FIXTURE: GrocyReferenceData = {
  products: [
    {
      id: 1,
      name: "Rolled oats",
      location_id: 10,
      qu_id_stock: 20,
      qu_id_purchase: 20,
      qu_id_consume: 20,
      min_stock_amount: 250,
      default_best_before_days: 180,
      due_type: 1,
      parent_product_id: null,
      product_group_id: 30,
      description: "Thin rolled oats.",
      calories: null,
    },
    {
      id: 2,
      name: "Almond butter",
      location_id: 10,
      qu_id_stock: 21,
      qu_id_purchase: 22,
      qu_id_consume: 22,
      min_stock_amount: 0,
      default_best_before_days: 180,
      due_type: 1,
      parent_product_id: null,
      product_group_id: null,
      description: null,
      calories: null,
    },
  ],
  locations: [
    { id: 10, name: "Pantry" },
    { id: 11, name: "Fridge" },
    { id: 12, name: "Freezer" },
  ],
  quantity_units: [
    { id: 20, name: "gram" },
    { id: 21, name: "jar" },
    { id: 22, name: "case" },
    { id: 23, name: "carton" },
  ],
  product_groups: [
    { id: 30, name: "Snacks" },
    { id: 31, name: "Grains" },
    { id: 32, name: "Dairy" },
  ],
  shopping_lists: [
    { id: 40, name: "Weekly" },
    { id: 41, name: "Costco run" },
  ],
  shopping_list_items: [
    { item_id: 3, product_name: "Milk", note: null, amount: 1, qu_name: "Carton", done: false },
    { item_id: 7, product_name: "Spinach", note: null, amount: 200, qu_name: "Gram", done: false },
    { item_id: 12, product_name: null, note: "paper towels?", amount: 1, qu_name: null, done: false },
    { item_id: 21, product_name: "Dark chocolate", note: null, amount: 2, qu_name: "Bar", done: true },
    { item_id: 42, product_name: "Almond butter", note: null, amount: 1, qu_name: "Jar", done: false },
    { item_id: 55, product_name: "Rolled oats", note: null, amount: 2, qu_name: "Pack", done: false },
  ],
};

export const GROCY_MCP_FIXTURES = {
  grocy_sf__products_list: () => GROCY_REFERENCE_FIXTURE.products,
  grocy_sf__locations_list: () => GROCY_REFERENCE_FIXTURE.locations,
  grocy_sf__quantity_units_list: () => GROCY_REFERENCE_FIXTURE.quantity_units,
  grocy_sf__product_groups_list: () => GROCY_REFERENCE_FIXTURE.product_groups,
  grocy_sf__shopping_lists_list: () => GROCY_REFERENCE_FIXTURE.shopping_lists,
  grocy_sf__shopping_list_get: (args: Record<string, unknown>) => ({
    items:
      args.shopping_list === GROCY_REFERENCE_FIXTURE.shopping_lists[0]?.id
        ? GROCY_REFERENCE_FIXTURE.shopping_list_items
        : [],
  }),
};
