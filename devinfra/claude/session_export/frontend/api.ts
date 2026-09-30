/**
 * The pairing page's API. Types come from the API's own OpenAPI document
 * (`//devinfra/claude/session_export/frontend:schema`), so the wire contract is the Pydantic
 * models rather than a hand-kept copy of them.
 */

import type { components } from "./api/schema";

export type SyncStatus = components["schemas"]["SyncStatus"];
export type PairingStart = components["schemas"]["PairingStart"];

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** FastAPI's `detail` is a string for an HTTPException and a list of issues for a rejected body. */
export function detailMessage(body: unknown, fallback: string): string {
  const detail = typeof body === "object" && body !== null ? (body as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((issue) => (issue as { msg?: unknown }).msg).filter((msg) => typeof msg === "string");
    if (messages.length > 0) return messages.join("; ");
  }
  return fallback;
}

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    if (response.status === 401) throw new ApiError(401, "Your login has expired. Reload the page to sign in again.");
    const failure: unknown = await response.json().catch(() => null);
    throw new ApiError(response.status, detailMessage(failure, `The server answered ${response.status}.`));
  }
  return response.status === 202 || response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export function getStatus(): Promise<SyncStatus> {
  return call("GET", "/api/status");
}

export function startPairing(): Promise<PairingStart> {
  return call("POST", "/api/pairing");
}

export function finishPairing(redirectUrl: string): Promise<SyncStatus> {
  return call("POST", "/api/pairing/complete", { redirect_url: redirectUrl });
}

export function syncNow(): Promise<void> {
  return call("POST", "/api/sync");
}
