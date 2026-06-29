import type { DashboardResponse, ImprovementsBoard, RunsResponse } from "./types.ts";

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

export async function fetchDashboard(): Promise<DashboardResponse> {
  const res = await fetch("/api/dashboard");
  if (!res.ok) throw new Error(await detail(res, "Failed to load dashboard"));
  return (await res.json()) as DashboardResponse;
}

export async function clickAction(itemId: string, actionId: string): Promise<void> {
  const res = await fetch(`/api/trace/items/${encodeURIComponent(itemId)}/actions/${encodeURIComponent(actionId)}`, {
    method: "PUT",
  });
  if (!res.ok) throw new Error(await detail(res, "Failed to record click"));
}

export async function unclickAction(itemId: string, actionId: string): Promise<void> {
  const res = await fetch(`/api/trace/items/${encodeURIComponent(itemId)}/actions/${encodeURIComponent(actionId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await detail(res, "Failed to retract click"));
}

export async function sendFeedback(text: string, itemId?: string): Promise<void> {
  const res = await fetch("/api/trace/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, item_id: itemId ?? null }),
  });
  if (!res.ok) throw new Error(await detail(res, "Failed to send feedback"));
}

export async function fetchImprovements(): Promise<ImprovementsBoard> {
  const res = await fetch("/api/improvements");
  if (!res.ok) throw new Error(await detail(res, "Failed to load improvements"));
  return (await res.json()) as ImprovementsBoard;
}

export async function fetchRuns(): Promise<RunsResponse> {
  const res = await fetch("/api/runs");
  if (!res.ok) throw new Error(await detail(res, "Failed to load runs"));
  return (await res.json()) as RunsResponse;
}
