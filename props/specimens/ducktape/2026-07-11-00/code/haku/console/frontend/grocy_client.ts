import { api, errorDetail } from "./client.ts";
import type { components } from "./api/schema";

export type GrocyReferenceResponse = components["schemas"]["GrocyReferenceResponse"];

// Live product/location/quantity-unit `{id, name}` lookup for rendering pending grocy-sf
// stock_add/stock_consume/products_create approvals — their arguments accept either a name
// or a numeric ID, and only names render nicely on their own.
export async function fetchGrocyReference(): Promise<GrocyReferenceResponse> {
  const { data, error } = await api.GET("/api/grocy-sf/reference");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load grocy-sf reference data"));
  return data;
}
