// Notification/card action descriptions for Grocy's tools — the one-line summary the
// approvals card shows and a push notification is titled with. Beside the widgets they
// describe, and React-free so `../../sw.ts` can bundle them (see ../action_entry.ts).

import { mcpToolSchema } from "../../mcp_tool_schema.ts";
import { fixed, fromArgs, plural } from "../action_entry.ts";
import type { ActionEntry } from "../action_entry.ts";
import { GROCY_SERVER_ID } from "../server_ids.ts";

export const grocyActions: Record<string, ActionEntry> = {
  stock_add: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "stock_add"), (a) => ({
    text: `Grocy: Add ${plural(a.items.length, "item")} to stock`,
  })),
  stock_consume: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "stock_consume"), (a) => ({
    text: `Grocy: Remove ${plural(a.items.length, "item")} from stock`,
  })),
  stock_entry_edit: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "stock_entry_edit"), (a) => ({
    text: `Grocy: Edit ${a.items.length} stock ${a.items.length === 1 ? "entry" : "entries"}`,
  })),
  stock_get: fixed("Grocy: View stock"),
  products_list: fixed("Grocy: List products"),
  quantity_units_list: fixed("Grocy: List quantity units"),
  get_system_info: fixed("Grocy: View system information"),
  products_create: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "products_create"), (a) => ({
    text: `Grocy: Create ${plural(a.items.length, "product")}`,
  })),
  products_edit: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "products_edit"), (a) => ({
    text: `Grocy: Edit ${plural(a.items.length, "product")}`,
  })),
  shopping_list_get: fixed("Grocy: View shopping list"),
  shopping_list_items_add: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "shopping_list_items_add"), (a) => ({
    text: `Grocy: Add ${plural(a.items.length, "item")} to shopping list`,
  })),
  shopping_list_items_remove: fromArgs(mcpToolSchema(GROCY_SERVER_ID, "shopping_list_items_remove"), (a) => ({
    text: `Grocy: Remove ${plural(a.item_ids.length, "shopping-list item")}`,
    destructive: true,
  })),
  shopping_list_item_edit: fixed("Grocy: Edit shopping list item"),
};
