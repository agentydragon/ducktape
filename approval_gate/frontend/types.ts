// Types matching the approval_gate Python models (approval_gate/models.py).
// Defined directly here — no generated schema needed now that the frontend
// uses MCP tool calls instead of the REST API.

export type ActionStatus = "pending" | "executing" | "done" | "rejected" | "withdrawn";

/** UUID string for an action (matches uuid.UUID in Python models). */
export type ActionId = string;

export type ToolCall = {
  server_namespace: string;
  tool_name: string;
  arguments: Record<string, unknown>;
};

export type PendingState = { status: "pending" };
export type ExecutingState = { status: "executing" };
export type DoneState = {
  status: "done";
  outcome: { isError?: boolean; content: unknown[] };
};
export type RejectedState = { status: "rejected"; reason: string | null };
export type WithdrawnState = { status: "withdrawn" };

export type ActionState = PendingState | ExecutingState | DoneState | RejectedState | WithdrawnState;

export type Action = {
  id: ActionId;
  created_at: string;
  updated_at: string;
  call: ToolCall;
  justification: string;
  session_key: string | null;
  state: ActionState;
};
