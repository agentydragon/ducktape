// Shared typed models mirroring backend API and WS payloads

export type AgentRow = {
  id: string;
  created_at?: string;
  live?: boolean;
  working?: boolean;
  last_updated?: string;
  metadata: { preset: string };
  lifecycle?: "persisted_only" | "starting" | "ready";
};

export type AgentListResponse = { agents: AgentRow[] };
export type McpServerState = "initializing" | "running" | "failed";
export type McpState = { entries: Record<string, ServerEntry> };
export type PolicyState = { version?: number | null };
export type UiStateLite = { ready: boolean };
export type ContainerState = { present: boolean; id?: string | null };

export type AgentStatus = {
  id: string;
  live: boolean;
  lifecycle?: "persisted_only" | "starting" | "ready" | "closing" | "closed" | "error";
  run_phase?: "idle" | "sampling" | "waiting_tool" | "tools_running" | "waiting_approval" | "sending_output" | "error";
  policy?: PolicyState;
  ui?: UiStateLite;
  mcp?: McpState;
  container?: ContainerState;
  pending_approvals?: number | null;
  last_event_at?: string | null;
};
export type DeleteResponse = { ok: boolean; error?: string };

// MCP tool definition as sent by backend
export type McpTool = {
  name: string;
  description?: string;
  // Matches backend payload shape
  inputSchema?: Record<string, any>;
};

// Align with InitializeResult (camelCase) from MCP types; the backend passes it as-is now.
export type ServerResourcesCaps = {
  resources?: { subscribe?: boolean; listChanged?: boolean } | null;
};

export type InitializeView = {
  instructions?: string | null;
  serverInfo?: any;
  capabilities?: ServerResourcesCaps | null;
};

export type ServerEntryInitializing = { name: string; state: "initializing" };

export type ServerEntryRunning = {
  name: string;
  state: "running";
  initialize: InitializeView;
  tools?: McpTool[];
};

export type ServerEntryFailed = { name: string; state: "failed"; error?: string | null };

export type ServerEntry = ServerEntryInitializing | ServerEntryRunning | ServerEntryFailed;

export type SamplingSnapshot = {
  ts?: string;
  servers: ServerEntry[];
};

// ---- Approval policy (shared) ----

export type PolicyError = {
  stage: "read" | "parse" | "tests";
  message?: string | null;
};

export type Proposal = {
  id: string;
  status?: "pending" | "approved" | "rejected";
};

export type ApprovalPolicyInfo = {
  content: string;
  version: number;
  proposals?: Proposal[];
};

export type SnapshotPayload = {
  type: "snapshot";
  approval_policy?: ApprovalPolicyInfo;
  sampling?: SamplingSnapshot;
};

// ---- UiState (server-owned) ----

export type ApprovalKind = "approve" | "deny_continue" | "deny_abort";

export type UserMessageItem = {
  kind: "UserMessage";
  id: string;
  ts: string;
  text: string;
};

export type AssistantMarkdownItem = {
  kind: "AssistantMarkdown";
  id: string;
  ts: string;
  md: string;
};

export type EndTurnItem = {
  kind: "EndTurn";
  id: string;
  ts: string;
};

export type ExecContent = {
  content_kind: "Exec";
  cmd?: string | null;
  args?: unknown | null;
  stdout?: string | null;
  stderr?: string | null;
  exit_code?: number | null;
  is_error?: boolean | null;
};

export type JsonContent = {
  content_kind: "Json";
  args?: unknown | null;
  result?: unknown | null;
  is_error?: boolean | null;
};

export type ToolContent = ExecContent | JsonContent;

export type ToolItem = {
  kind: "Tool";
  id: string;
  ts: string;
  tool: string;
  call_id: string;
  decision?: ApprovalKind | null;
  content: ToolContent;
};

export type UiDisplayItem = UserMessageItem | AssistantMarkdownItem | EndTurnItem | ToolItem;

export type UiState = {
  seq: number;
  items: UiDisplayItem[];
};

export type UiStateSnapshotPayload = { type: "ui_state_snapshot"; state: UiState };
export type UiStateUpdatedPayload = { type: "ui_state_updated"; state: UiState };
export type ApprovalPendingPayload = {
  type: "approval_pending";
  call_id: string;
  tool_key: string;
  args_json?: string | null;
};
export type ApprovalDecisionPayload = {
  type: "approval_decision";
  call_id: string;
  decision: ApprovalKind;
};
export type AcceptedPayload = { type: "accepted" };
export type ErrorPayload = { type: "error"; code: string; message?: string };

// Optional resource-updated notification proxy (backend may emit in future)

export type IncomingPayload =
  | SnapshotPayload
  | UiStateSnapshotPayload
  | UiStateUpdatedPayload
  | ApprovalPendingPayload
  | ApprovalDecisionPayload
  | AcceptedPayload
  | ErrorPayload;
