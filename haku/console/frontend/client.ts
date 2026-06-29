import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";

// Same-origin typed client (nginx serves this bundle and proxies /api). Types are
// generated from the backend's OpenAPI schema: //haku/console/frontend:schema.
const api = createClient<paths>({ baseUrl: "" });

export type ConfigResponse = components["schemas"]["ConfigResponse"];
export type LaunchRoutineResult = components["schemas"]["LaunchRoutineResult"];

// FastAPI error responses are `{detail: string}`; surface that real reason rather
// than a generic message, falling back when the body isn't shaped that way.
function errorDetail(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "detail" in error) {
    const { detail } = error as { detail: unknown };
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

export async function fetchConfig(): Promise<ConfigResponse> {
  const { data, error } = await api.GET("/api/config");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load config"));
  return data;
}

// Capability tier. Fetch a CSRF token (which also sets the signed double-submit
// cookie), then fire the routine echoing the token in X-CSRF-Token. The bearer
// stays server-side; this only triggers the action and returns the new session URL.
export async function launchRoutine(text?: string): Promise<LaunchRoutineResult> {
  const { data: csrf, error: csrfError } = await api.GET("/api/capabilities/csrf");
  if (csrfError || !csrf) throw new Error(errorDetail(csrfError, "Failed to get CSRF token"));
  const { data, error } = await api.POST("/api/capabilities/launch-routine", {
    headers: { "X-CSRF-Token": csrf.csrf_token },
    body: text ? { text } : {},
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to launch routine"));
  return data;
}
