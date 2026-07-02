// Wire contract with the backend's JSON API. Mirrors the Pydantic models in
// ../../backend/models.py, which themselves mirror haku/base/schema/item.json.
// Keep this in sync with models.py when the item shape changes.

export type PrimaryAction = { kind: "suggestion" } | { kind: "prepared_prompt"; prompt: string };

export type OperatorAction =
  | { kind: "command"; id: string; label: string; intent: string }
  | { kind: "claude_handoff"; id: string; label: string; prompt: string };

export type ItemStatus = "open" | "in_progress" | "done" | "rejected" | "snoozed" | "expired";

export interface Item {
  id: string;
  title: string;
  body: string;
  value: number;
  action: PrimaryAction | null;
  status: ItemStatus;
  deadline: string | null;
  actions: OperatorAction[];
}

export interface Click {
  item_id: string;
  action_id: string;
}

export interface DashboardResponse {
  scan_time: string; // ISO 8601 of the last haku-state commit
  deployed_commit: string | null; // short SHA the running image was built from
  deployed_commit_url: string | null; // Forgejo link to that commit
  items: Item[];
  clicks: Click[];
}

// --- Improvements / friction surface (improvements.yaml) ---
// Haku's self-backlog: capability ideas it could grow into + friction it hits during runs.
export interface ImprovementIdea {
  id: string;
  title: string;
  value: "high" | "medium" | "low";
  status: "recommend" | "idea" | "parked" | "blocked" | "wired";
  summary: string;
  detail: string; // markdown
}

export interface Friction {
  id: string;
  title: string;
  severity: "high" | "medium" | "low";
  status: "open" | "workaround" | "resolved" | "answered";
  detail: string; // markdown
}

export interface ImprovementsBoard {
  updated: string;
  ideas: ImprovementIdea[];
  friction: Friction[];
}

// --- Runs surface (runs/<date>/<ulid>.{yaml,md}) ---
// Per-run propagation record: each source processed + how each change reached every surface.
// A source was either scanned (bookmarks + count) or skipped (reason) — a discriminated union
// (mirrors the backend's ScannedSource | SkippedSource); discriminate on the `skipped` key.
export interface ScannedSource {
  source: string;
  bookmark_before: string | number | null;
  bookmark_after: string | number | null;
  changes_seen: number | string;
}

export interface SkippedSource {
  source: string;
  skipped: string;
}

export type RunSource = ScannedSource | SkippedSource;

export interface RunChecklist {
  checklist: string;
  ref: string;
  walked: boolean;
}

export interface PropagationTarget {
  surface: string;
  action: "created" | "updated" | "no_change" | "n/a";
  note: string;
}

export interface PropagationEntry {
  change: string;
  source: string;
  surfaces: PropagationTarget[];
}

export interface RunManifest {
  run_id: string;
  date: string;
  started: string;
  finished: string;
  sources: RunSource[];
  checklists: RunChecklist[];
  propagation: PropagationEntry[];
  notes_md: string;
}

export interface RunsResponse {
  runs: RunManifest[];
}

// --- Knowledge garden (arbitrary repo markdown under whitelisted dirs) ---
export interface GardenEntry {
  path: string; // repo-relative, e.g. "memory/situational-awareness.md"
}

export interface GardenIndex {
  entries: GardenEntry[];
}

export interface GardenFile {
  path: string;
  markdown: string; // raw .md/.mdx source; rendered client-side as MDX
}
