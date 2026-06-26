import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";

// Same-origin typed client (the FastAPI backend serves this bundle). Types are
// generated from the backend's OpenAPI schema: //haku/console/frontend:schema.
const api = createClient<paths>({ baseUrl: "" });

export type Item = components["schemas"]["Item"];
export type DashboardResponse = components["schemas"]["DashboardResponse"];
export type LaunchRoutineResult = components["schemas"]["LaunchRoutineResult"];
export type OperatorAction = NonNullable<Item["actions"]>[number];
export type PrimaryAction = Item["action"];

// FastAPI error responses are `{detail: string}`; surface that real reason rather
// than a generic message, falling back when the body isn't shaped that way.
function errorDetail(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "detail" in error) {
    const { detail } = error as { detail: unknown };
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

export async function fetchDashboard(): Promise<DashboardResponse> {
  const { data, error } = await api.GET("/api/dashboard");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load dashboard"));
  return data;
}

export async function clickAction(itemId: string, actionId: string): Promise<void> {
  const { error } = await api.PUT("/api/trace/items/{item_id}/actions/{action_id}", {
    params: { path: { item_id: itemId, action_id: actionId } },
  });
  if (error) throw new Error(errorDetail(error, "Failed to record click"));
}

export async function unclickAction(itemId: string, actionId: string): Promise<void> {
  const { error } = await api.DELETE("/api/trace/items/{item_id}/actions/{action_id}", {
    params: { path: { item_id: itemId, action_id: actionId } },
  });
  if (error) throw new Error(errorDetail(error, "Failed to retract click"));
}

export async function sendFeedback(text: string, itemId?: string): Promise<void> {
  const { error } = await api.POST("/api/trace/feedback", { body: { text, item_id: itemId } });
  if (error) throw new Error(errorDetail(error, "Failed to send feedback"));
}

// Capability tier. Fetch a CSRF token (which also sets the signed double-submit
// cookie), then fire the routine echoing the token in X-CSRF-Token. The bearer
// stays server-side; this only triggers the action and returns the new session URL.
export async function launchRoutine(): Promise<LaunchRoutineResult> {
  const { data: csrf, error: csrfError } = await api.GET("/api/capabilities/csrf");
  if (csrfError || !csrf) throw new Error(errorDetail(csrfError, "Failed to get CSRF token"));
  const { data, error } = await api.POST("/api/capabilities/launch-routine", {
    headers: { "X-CSRF-Token": csrf.csrf_token },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to launch routine"));
  return data;
}
