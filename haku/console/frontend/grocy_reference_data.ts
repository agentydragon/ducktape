/** Optional reference values for readable Grocy previews when they are carried by a call record. */
type ReferenceItem = { id: number; name: string };

type Product = {
  id: number;
  name: string;
  location_id: number;
  qu_id_stock: number;
  qu_id_purchase: number;
  qu_id_consume: number;
  min_stock_amount: number;
  default_best_before_days: number;
  due_type: number;
  parent_product_id: number | null;
  product_group_id: number | null;
  description: string | null;
  calories: number | null;
};

type ShoppingListItem = {
  item_id: number;
  product_name: string | null;
  note: string | null;
  amount: number;
  qu_name: string | null;
  done: boolean;
};

export type GrocyReferenceData = {
  products: Product[];
  locations: ReferenceItem[];
  quantity_units: ReferenceItem[];
  product_groups: ReferenceItem[];
  shopping_lists: ReferenceItem[];
  shopping_list_items: ShoppingListItem[];
};
