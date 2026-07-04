import { parse } from "yaml";

import { invalidateTree, readBlobs, repoFile } from "./repo.ts";
import type { FeedbackContext, MetaResponse, RunManifest, RunsResponse } from "./types.ts";

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

// Recent per-run propagation manifests, composed over the shared git-store reader (no bespoke
// /api/runs): pair each `runs/<date>/<ulid>.yaml` with its sibling `.md` prose notes (README.md
// and dangling `.md` ignored), newest-first by `started`. Missing optional fields default (mirrors
// the backend RunManifest) so one lean manifest can't crash the table.
const EMPTY_RUN: Omit<RunManifest, "run_id" | "notes_md"> = {
  date: "",
  started: "",
  finished: "",
  sources: [],
  checklists: [],
  propagation: [],
};

export async function fetchRuns(limit = 20): Promise<RunsResponse> {
  const blobs = await readBlobs(
    (e) =>
      e.path.startsWith("runs/") &&
      (e.path.endsWith(".yaml") || (e.path.endsWith(".md") && !e.path.endsWith("/README.md")))
  );
  // Pair each runs/<date>/<ulid>.yaml manifest with its sibling .md notes (by shared base path).
  const yamls = new Map<string, string>(); // base → manifest yaml
  const notes = new Map<string, string>(); // base → prose notes
  for (const b of blobs) {
    if (b.path.endsWith(".yaml")) yamls.set(b.path.slice(0, -".yaml".length), b.content);
    else notes.set(b.path.slice(0, -".md".length), b.content);
  }
  const runs = [...yamls]
    .map(([base, yamlText]): RunManifest => {
      const m = (parse(yamlText) ?? {}) as Partial<RunManifest>;
      return { ...EMPTY_RUN, ...m, run_id: m.run_id ?? base, notes_md: notes.get(base) ?? "" };
    })
    .sort((a, b) => b.started.localeCompare(a.started));
  return { runs: runs.slice(0, limit) };
}

// Garden browse + file read compose over the shared git-store reader (repo.ts: readBlobs/repoFile)
// — no bespoke garden endpoints.
