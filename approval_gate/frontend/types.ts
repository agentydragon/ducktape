// Types matching the approval_gate Python models (approval_gate/models.py).
// Defined directly here — no generated schema needed now that the frontend
// uses MCP tool calls instead of the REST API.

export type ActionStatus = "pending" | "executing" | "done" | "rejected" | "withdrawn";

export type ActionKey = {
  session_key: string;
  action_seq: number;
};

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
  key: ActionKey;
  created_at: string;
  updated_at: string;
  call: ToolCall;
  justification: string;
  state: ActionState;
};

export type LogEventKind =
  | "action_received"
  | "approved"
  | "denied"
  | "withdrawn"
  | "execution_started"
  | "execution_finished";

export type LogEntry = {
  entry_id: number;
  session_key: string;
  action_seq: number;
  kind: LogEventKind;
  timestamp: string;
  detail_json: string | null;
};
