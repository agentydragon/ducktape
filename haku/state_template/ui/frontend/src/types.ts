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
