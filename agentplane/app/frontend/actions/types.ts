/** The integration app's presentation model for Action Service protobuf receipts. */

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type ActionState =
  | "decision_pending"
  | "allowed"
  | "denied"
  | "dispatching"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "execution_unknown";

export type ExecutionState = Exclude<ActionState, "decision_pending" | "allowed" | "denied"> | "pending_dispatch";
export type Verdict = "allow" | "deny";

export interface ActionIdentity {
  group: string;
  name: string;
}

export interface ServiceAccountRef {
  namespace: string;
  name: string;
}

export interface OperatorView {
  issuer: string;
  subject: string;
}

export interface PolicyEvidence {
  bindings: { namespace: string; name: string; resource_version: string }[];
  policy_sets: { namespace: string; name: string; generation: number }[];
  matched: {
    namespace: string;
    policy_set: string;
    source: string;
    index: number;
    type: string;
    repository: { owner: string; repository: string; confirmed_public: boolean } | null;
  };
}

export interface DecisionView {
  id: string;
  verdict: Verdict;
  provider: string;
  operator: OperatorView | null;
  decision_note: string | null;
  reason_code: string | null;
  reason_description: string | null;
  policy_evidence: PolicyEvidence | null;
  idempotency_key: string;
  decided_at: string;
}

export interface ExecutionView {
  id: string;
  state: ExecutionState;
  result: JsonValue | null;
  error: { [key: string]: JsonValue } | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  reconciled_at: string | null;
}

export interface ActionRequestView {
  id: string;
  idempotency_key: string;
  action: ActionIdentity;
  arguments: { [key: string]: JsonValue };
  title: string;
  description: string | null;
  caller: ServiceAccountRef;
  state: ActionState;
  version: number;
  created_at: string;
  updated_at: string;
  decision: DecisionView | null;
  execution: ExecutionView | null;
}

export interface ActionHistoryPage {
  items: ActionRequestView[];
  next_cursor: string | null;
}

export type McpLifecycle = "disconnected" | "connecting" | "discovering" | "available" | "draining" | "stopped";

export type McpUnavailableReason =
  | "connect_failed"
  | "discovery_failed"
  | "invalid_catalog"
  | "linkage_unavailable"
  | "session_failed"
  | "supervisor_stopped";

export interface McpHealthView {
  state: McpLifecycle;
  reason: McpUnavailableReason | null;
  detail: string | null;
  last_discovery_at: string | null;
  retry_at: string | null;
  failures: number;
}

export interface ActionGroupView {
  key: string;
  title: string;
  description: string;
  executor_kind: "mcp" | "pod" | "orchestration_v1";
  executor_description: string;
  available: boolean;
  health: McpHealthView | null;
  actions: {
    group: string;
    name: string;
    description: string;
    input_schema: { [key: string]: JsonValue };
    title: string | null;
    annotations: {
      read_only_hint: boolean | null;
      destructive_hint: boolean | null;
      idempotent_hint: boolean | null;
      open_world_hint: boolean | null;
    } | null;
  }[];
  direct_tools: string[];
}
