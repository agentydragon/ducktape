import { parse } from "yaml";

import { invalidateTree, repoFile } from "./repo.ts";
import type { FeedbackContext, MetaResponse, RunsResponse } from "./types.ts";

// Same-origin JSON client: the FastAPI backend serves this bundle and the API.
// FastAPI error responses are `{detail: string}`; surface that real reason.

async function detail(res: Response, fallback: string): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // non-JSON body; fall through to the generic message
  }
  return fallback;
}

export async function fetchMeta(): Promise<MetaResponse> {
  const res = await fetch("/api/meta");
  if (!res.ok) throw new Error(await detail(res, "Failed to load metadata"));
  return (await res.json()) as MetaResponse;
}

export async function sendFeedback(text: string, itemId?: string, context?: FeedbackContext): Promise<void> {
  // Only attach the page/selection when we actually have them (page whenever a context was
  // captured; selection only when non-empty), so the note stays clean when there's nothing extra.
  const body: Record<string, unknown> = { text, item_id: itemId ?? null };
  if (context) {
    body.page = context.page;
    if (context.selection) body.selection = context.selection;
  }
  const res = await fetch("/api/trace/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await detail(res, "Failed to send feedback"));
  invalidateTree();
}

// Responses log (plans/ui-authoring-architecture → feedback loop): a keyed current-state file per
// (scope, field) slot. Write via the dedicated endpoint; read the current answer through the proxy
// (repoFile at HEAD). The commit history is the append-only log; the file is the projection.
export async function setResponse(scope: string, field: string, value: string, note?: string): Promise<void> {
  const res = await fetch(`/api/responses/${encodeURIComponent(scope)}/${encodeURIComponent(field)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value, note: note ?? null }),
  });
  if (!res.ok) throw new Error(await detail(res, "Failed to record response"));
  invalidateTree();
}

export async function clearResponse(scope: string, field: string): Promise<void> {
  const res = await fetch(`/api/responses/${encodeURIComponent(scope)}/${encodeURIComponent(field)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await detail(res, "Failed to clear response"));
  invalidateTree();
}

// Current answer for a slot: read responses/<scope>/<field>.yaml at HEAD (null if unanswered).
export async function readResponse(scope: string, field: string): Promise<string | null> {
  const content = await repoFile(`responses/${scope}/${field}.yaml`);
  if (content === null) return null;
  const parsed = (parse(content) ?? {}) as { value?: unknown };
  return typeof parsed.value === "string" ? parsed.value : null;
}

export async function fetchRuns(): Promise<RunsResponse> {
  const res = await fetch("/api/runs");
  if (!res.ok) throw new Error(await detail(res, "Failed to load runs"));
  return (await res.json()) as RunsResponse;
}

// Garden browse + file read now compose over the generic content proxy (repo.ts:
// repoTree/repoFile) — no bespoke garden endpoints.
