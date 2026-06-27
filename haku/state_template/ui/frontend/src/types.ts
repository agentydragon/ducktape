// Wire contract with the backend's JSON API. Mirrors the Pydantic models in
// ../../backend/models.py, which themselves mirror haku/base/schema/item.json.
// (The console generated these from its OpenAPI schema; here we hand-maintain them
// — keep them in sync with models.py when the item shape changes.)

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
  scan_time: string;
  items: Item[];
  clicks: Click[];
}
