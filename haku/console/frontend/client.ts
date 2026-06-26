import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";

// Same-origin typed client (the FastAPI backend serves this bundle). Types are
// generated from the backend's OpenAPI schema: //haku/console/frontend:schema.
const api = createClient<paths>({ baseUrl: "" });

export type Item = components["schemas"]["Item"];
export type DashboardResponse = components["schemas"]["DashboardResponse"];
export type OperatorAction = NonNullable<Item["actions"]>[number];
export type PrimaryAction = Item["action"];

export async function fetchDashboard(): Promise<DashboardResponse> {
  const { data, error } = await api.GET("/api/dashboard");
  if (error || !data) throw new Error("Failed to load dashboard");
  return data;
}

export async function clickAction(itemId: string, actionId: string): Promise<void> {
  const { error } = await api.PUT("/api/trace/items/{item_id}/actions/{action_id}", {
    params: { path: { item_id: itemId, action_id: actionId } },
  });
  if (error) throw new Error("Failed to record click");
}

export async function unclickAction(itemId: string, actionId: string): Promise<void> {
  const { error } = await api.DELETE("/api/trace/items/{item_id}/actions/{action_id}", {
    params: { path: { item_id: itemId, action_id: actionId } },
  });
  if (error) throw new Error("Failed to retract click");
}

export async function sendFeedback(text: string, itemId?: string): Promise<void> {
  const { error } = await api.POST("/api/trace/feedback", { body: { text, item_id: itemId } });
  if (error) throw new Error("Failed to send feedback");
}

// Capability tier. Fetch a CSRF token (which also sets the signed double-submit
// cookie), then fire the routine echoing the token in X-CSRF-Token. The bearer
// stays server-side; this only triggers the action.
export async function launchRoutine(): Promise<void> {
  const { data, error: csrfError } = await api.GET("/api/capabilities/csrf");
  if (csrfError || !data) throw new Error("Failed to get CSRF token");
  const { error } = await api.POST("/api/capabilities/launch-routine", {
    headers: { "X-CSRF-Token": data.csrf_token },
  });
  if (error) throw new Error("Failed to launch routine");
}
