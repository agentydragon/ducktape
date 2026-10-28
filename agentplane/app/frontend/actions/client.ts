import { create, fromJson, toJson, type JsonValue as ProtoJsonValue } from "@bufbuild/protobuf";
import { TimestampSchema, ValueSchema, type Timestamp, type Value } from "@bufbuild/protobuf/wkt";

import { fetchWithLogin, HttpError } from "../client";
import type { StreamConnection } from "../live_stream";
import { followWatch } from "../watch_stream";
import {
  ActionState as ProtoActionState,
  ExecutorKind as ProtoExecutorKind,
  ExecutionState as ProtoExecutionState,
  McpLifecycle as ProtoMcpLifecycle,
  McpUnavailableReason as ProtoMcpUnavailableReason,
  PolicyKind as ProtoPolicyKind,
  PolicySource as ProtoPolicySource,
  Verdict as ProtoVerdict,
  GetActionRequestResponseSchema,
  ListActionGroupsResponseSchema,
  ListActionRequestHistoryResponseSchema,
  ListActionRequestsRequestSchema,
  ListActionRequestsResponseSchema,
  WatchActionGroupsResponseSchema,
  WatchActionRequestsResponseSchema,
  type Action,
  type ActionGroup,
  type ActionRequest,
  type BindingEvidence as ProtoBindingEvidence,
  type Decision as ProtoDecision,
  type Execution as ProtoExecution,
  type MatchedPolicy as ProtoMatchedPolicy,
  type PolicyEvidence as ProtoPolicyEvidence,
  type PolicySetEvidence as ProtoPolicySetEvidence,
} from "../../../action_service/action_service_pb";
import {
  DecideActionRequestRequestSchema,
  DecideActionRequestResponseSchema,
} from "../../../action_service/operator_action_service_pb";
import type {
  ActionGroupView,
  ActionHistoryPage,
  ActionRequestView,
  ActionState,
  DecisionView,
  ExecutionView,
  JsonValue,
  McpHealthView,
  McpLifecycle,
  McpUnavailableReason,
  PolicyEvidence,
  Verdict,
} from "./types";

export type { ActionGroupView, ActionHistoryPage, ActionRequestView, ActionState, Verdict } from "./types";
type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const ACTIONS_PATH = "/upstream/actions";
function required<T>(value: T | undefined, field: string): T {
  if (value === undefined) throw new Error(`The Action Service omitted ${field}.`);
  return value;
}

function structJson(value: { [key: string]: ProtoJsonValue } | undefined): { [key: string]: JsonValue } {
  if (value === undefined) return {};
  const result: { [key: string]: JsonValue } = {};
  for (const [key, child] of Object.entries(value)) result[key] = child;
  return result;
}

function valueJson(value: Value | undefined): JsonValue | null {
  return value === undefined ? null : toJson(ValueSchema, value);
}

function timestampJson(value: Timestamp | undefined, field: string): string {
  const json = toJson(TimestampSchema, required(value, field));
  if (typeof json !== "string") throw new Error(`The Action Service returned an invalid ${field}.`);
  return json;
}

function optionalTimestampJson(value: Timestamp | undefined): string | null {
  return value === undefined ? null : timestampJson(value, "timestamp");
}

function safeInt64(value: bigint, field: string): number {
  const number = Number(value);
  if (!Number.isSafeInteger(number)) throw new Error(`The Action Service returned an unsafe ${field}.`);
  return number;
}

function actionState(value: ProtoActionState): ActionState {
  switch (value) {
    case ProtoActionState.DECISION_PENDING:
      return "decision_pending";
    case ProtoActionState.ALLOWED:
      return "allowed";
    case ProtoActionState.DENIED:
      return "denied";
    case ProtoActionState.DISPATCHING:
      return "dispatching";
    case ProtoActionState.RUNNING:
      return "running";
    case ProtoActionState.SUCCEEDED:
      return "succeeded";
    case ProtoActionState.FAILED:
      return "failed";
    case ProtoActionState.CANCELLED:
      return "cancelled";
    case ProtoActionState.EXECUTION_UNKNOWN:
      return "execution_unknown";
    default:
      throw new Error(`The Action Service returned an unknown request state (${value}).`);
  }
}

function verdict(value: ProtoVerdict): Verdict {
  switch (value) {
    case ProtoVerdict.ALLOW:
      return "allow";
    case ProtoVerdict.DENY:
      return "deny";
    default:
      throw new Error(`The Action Service returned an unknown verdict (${value}).`);
  }
}

function policySource(value: ProtoPolicySource): string {
  switch (value) {
    case ProtoPolicySource.RULES:
      return "rules";
    default:
      throw new Error(`The Action Service returned an unknown policy source (${value}).`);
  }
}

function policyKind(value: ProtoPolicyKind): string {
  switch (value) {
    case ProtoPolicyKind.EXACT_ACTIONS:
      return "exact_actions";
    case ProtoPolicyKind.ARGUMENT_SCHEMA:
      return "argument_schema";
    case ProtoPolicyKind.GITHUB_REPOSITORY:
      return "github_repository";
    case ProtoPolicyKind.GITHUB_PUBLIC_REPOSITORY:
      return "github_public_repository";
    case ProtoPolicyKind.HOME_ASSISTANT_ENTITY_CONTROL:
      return "home_assistant_entity_control";
    default:
      throw new Error(`The Action Service returned an unknown policy kind (${value}).`);
  }
}

function requestPolicyEvidence(value: ProtoPolicyEvidence | undefined): PolicyEvidence | null {
  if (value === undefined) return null;
  const matched = required(value.matched, "policy evidence match");
  return {
    bindings: value.bindings.map(bindingEvidence),
    policy_sets: value.policySets.map(policySetEvidence),
    matched: matchedPolicy(matched),
  };
}

function bindingEvidence(value: ProtoBindingEvidence): PolicyEvidence["bindings"][number] {
  return { namespace: value.namespace, name: value.name, resource_version: value.resourceVersion };
}

function policySetEvidence(value: ProtoPolicySetEvidence): PolicyEvidence["policy_sets"][number] {
  return { namespace: value.namespace, name: value.name, generation: safeInt64(value.generation, "policy generation") };
}

function matchedPolicy(value: ProtoMatchedPolicy): PolicyEvidence["matched"] {
  return {
    namespace: value.namespace,
    policy_set: value.policySet,
    source: policySource(value.source),
    index: value.index,
    type: policyKind(value.type),
    repository:
      value.repository === undefined
        ? null
        : {
            owner: value.repository.owner,
            repository: value.repository.repository,
            confirmed_public: value.repository.confirmedPublic,
          },
  };
}

function decisionView(value: ProtoDecision): DecisionView {
  return {
    id: value.id,
    verdict: verdict(value.verdict),
    provider: value.provider,
    operator: value.operator === undefined ? null : { issuer: value.operator.issuer, subject: value.operator.subject },
    decision_note: value.decisionNote ?? null,
    reason_code: value.reasonCode ?? null,
    reason_description: value.reasonDescription ?? null,
    policy_evidence: requestPolicyEvidence(value.policyEvidence),
    idempotency_key: value.idempotencyKey,
    decided_at: timestampJson(value.decidedAt, "Decision.decided_at"),
  };
}

function executionState(value: ProtoExecutionState): ExecutionView["state"] {
  switch (value) {
    case ProtoExecutionState.PENDING_DISPATCH:
      return "pending_dispatch";
    case ProtoExecutionState.DISPATCHING:
      return "dispatching";
    case ProtoExecutionState.RUNNING:
      return "running";
    case ProtoExecutionState.SUCCEEDED:
      return "succeeded";
    case ProtoExecutionState.FAILED:
      return "failed";
    case ProtoExecutionState.CANCELLED:
      return "cancelled";
    case ProtoExecutionState.EXECUTION_UNKNOWN:
      return "execution_unknown";
    default:
      throw new Error(`The Action Service returned an unknown execution state (${value}).`);
  }
}

function executionView(value: ProtoExecution): ExecutionView {
  return {
    id: value.id,
    state: executionState(value.state),
    result: valueJson(value.result),
    error: value.error === undefined ? null : structJson(value.error),
    created_at: timestampJson(value.createdAt, "Execution.created_at"),
    started_at: optionalTimestampJson(value.startedAt),
    completed_at: optionalTimestampJson(value.completedAt),
    reconciled_at: optionalTimestampJson(value.reconciledAt),
  };
}

function actionRequestFromMessage(message: ActionRequest): ActionRequestView {
  const action = required(message.action, "ActionRequest.action");
  const caller = required(message.caller, "ActionRequest.caller");
  return {
    id: message.id,
    idempotency_key: message.idempotencyKey,
    action: { group: action.group, name: action.name },
    arguments: structJson(message.arguments),
    title: message.title,
    description: message.description ?? null,
    caller: { namespace: caller.namespace, name: caller.name },
    state: actionState(message.state),
    version: safeInt64(message.version, "request version"),
    created_at: timestampJson(message.createdAt, "ActionRequest.created_at"),
    updated_at: timestampJson(message.updatedAt, "ActionRequest.updated_at"),
    decision: message.decision === undefined ? null : decisionView(message.decision),
    execution: message.execution === undefined ? null : executionView(message.execution),
  };
}

function executorKind(value: ProtoExecutorKind): ActionGroupView["executor_kind"] {
  switch (value) {
    case ProtoExecutorKind.MCP:
      return "mcp";
    case ProtoExecutorKind.POD:
      return "pod";
    case ProtoExecutorKind.ORCHESTRATION_V1:
      return "orchestration_v1";
    default:
      throw new Error(`The Action Service returned an unknown executor kind (${value}).`);
  }
}

function mcpLifecycle(value: ProtoMcpLifecycle): McpLifecycle {
  switch (value) {
    case ProtoMcpLifecycle.DISCONNECTED:
      return "disconnected";
    case ProtoMcpLifecycle.CONNECTING:
      return "connecting";
    case ProtoMcpLifecycle.DISCOVERING:
      return "discovering";
    case ProtoMcpLifecycle.AVAILABLE:
      return "available";
    case ProtoMcpLifecycle.DRAINING:
      return "draining";
    case ProtoMcpLifecycle.STOPPED:
      return "stopped";
    default:
      throw new Error(`The Action Service returned an unknown MCP lifecycle state (${value}).`);
  }
}

function mcpUnavailableReason(value: ProtoMcpUnavailableReason): McpUnavailableReason | null {
  switch (value) {
    case ProtoMcpUnavailableReason.CONNECT_FAILED:
      return "connect_failed";
    case ProtoMcpUnavailableReason.DISCOVERY_FAILED:
      return "discovery_failed";
    case ProtoMcpUnavailableReason.INVALID_CATALOG:
      return "invalid_catalog";
    case ProtoMcpUnavailableReason.LINKAGE_UNAVAILABLE:
      return "linkage_unavailable";
    case ProtoMcpUnavailableReason.SESSION_FAILED:
      return "session_failed";
    case ProtoMcpUnavailableReason.SUPERVISOR_STOPPED:
      return "supervisor_stopped";
    case ProtoMcpUnavailableReason.UNSPECIFIED:
      return null;
    default:
      throw new Error(`The Action Service returned an unknown MCP unavailable reason (${value}).`);
  }
}

function mcpHealth(value: ActionGroup["health"]): McpHealthView | null {
  if (value === undefined) return null;
  return {
    state: mcpLifecycle(value.state),
    reason: value.reason === undefined ? null : mcpUnavailableReason(value.reason),
    detail: value.detail ?? null,
    last_discovery_at: optionalTimestampJson(value.lastDiscoveryAt),
    retry_at: optionalTimestampJson(value.retryAt),
    failures: value.failures,
  };
}

function actionView(value: Action): ActionGroupView["actions"][number] {
  const annotations = value.annotations;
  return {
    group: value.group,
    name: value.name,
    description: value.description,
    input_schema: structJson(value.inputSchema),
    title: value.title ?? null,
    annotations:
      annotations === undefined
        ? null
        : {
            read_only_hint: annotations.readOnlyHint ?? null,
            destructive_hint: annotations.destructiveHint ?? null,
            idempotent_hint: annotations.idempotentHint ?? null,
            open_world_hint: annotations.openWorldHint ?? null,
          },
  };
}

function actionGroupFromMessage(message: ActionGroup): ActionGroupView {
  return {
    key: message.key,
    title: message.title,
    description: message.description,
    executor_kind: executorKind(message.executorKind),
    executor_description: message.executorDescription,
    available: message.available,
    health: mcpHealth(message.health),
    actions: message.actions.map(actionView),
    direct_tools: message.directTools,
  };
}

function protoJson(value: unknown): ProtoJsonValue {
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (Array.isArray(value)) return value.map(protoJson);
  if (typeof value === "object") {
    const result: { [key: string]: ProtoJsonValue } = {};
    for (const [key, child] of Object.entries(value)) result[key] = protoJson(child);
    return result;
  }
  throw new Error("The Action Service returned a non-JSON value.");
}

async function readJson(
  url: string,
  init: RequestInit = {},
  fetcher: Fetcher = fetchWithLogin
): Promise<ProtoJsonValue> {
  const response = await fetcher(url, init);
  if (!response.ok) throw new HttpError(response, await response.text());
  return protoJson(await response.json());
}

function actionStateJson(value: ProtoActionState): string {
  const json = toJson(ListActionRequestsRequestSchema, create(ListActionRequestsRequestSchema, { state: [value] }));
  if (json === null || Array.isArray(json) || typeof json !== "object") {
    throw new Error("Could not encode the Action state.");
  }
  const states = json.state;
  if (!Array.isArray(states) || typeof states[0] !== "string") throw new Error("Could not encode the Action state.");
  return states[0];
}

function protoVerdict(value: Verdict): ProtoVerdict {
  switch (value) {
    case "allow":
      return ProtoVerdict.ALLOW;
    case "deny":
      return ProtoVerdict.DENY;
  }
}

export async function listActionRequests(
  state?: "decision_pending",
  fetcher: Fetcher = fetchWithLogin
): Promise<ActionRequestView[]> {
  const query = new URLSearchParams();
  if (state !== undefined) query.set("state", actionStateJson(ProtoActionState.DECISION_PENDING));
  const json = await readJson(`${ACTIONS_PATH}/v1/action-requests${query.size ? `?${query}` : ""}`, {}, fetcher);
  const response = fromJson(ListActionRequestsResponseSchema, json);
  return response.actionRequests.map(actionRequestFromMessage);
}

export async function listActionGroups(): Promise<ActionGroupView[]> {
  const response = fromJson(ListActionGroupsResponseSchema, await readJson(`${ACTIONS_PATH}/v1/action-groups`));
  return response.actionGroups.map(actionGroupFromMessage);
}

export async function getAction(requestId: string): Promise<ActionRequestView> {
  const response = fromJson(
    GetActionRequestResponseSchema,
    await readJson(`${ACTIONS_PATH}/v1/action-requests/${encodeURIComponent(requestId)}`)
  );
  if (response.actionRequest === undefined) throw new Error("The Action Service returned no request.");
  return actionRequestFromMessage(response.actionRequest);
}

export async function actionHistory(cursor?: string, includeAutoAllowed = true): Promise<ActionHistoryPage> {
  const query = new URLSearchParams({ limit: "50", includeAutoAllowed: String(includeAutoAllowed) });
  if (cursor !== undefined) query.set("cursor", cursor);
  const response = fromJson(
    ListActionRequestHistoryResponseSchema,
    await readJson(`${ACTIONS_PATH}/v1/action-requests:history?${query}`)
  );
  return {
    items: response.actionRequests.map(actionRequestFromMessage),
    next_cursor: response.nextCursor ?? null,
  };
}

export async function decideActionById(
  requestId: string,
  expectedVersion: number,
  verdict: Verdict,
  fetcher: Fetcher = fetchWithLogin
): Promise<ActionRequestView> {
  if (!Number.isSafeInteger(expectedVersion) || expectedVersion < 1) {
    throw new Error("The reviewed Action version is outside the supported integer range.");
  }
  const decision = create(DecideActionRequestRequestSchema, {
    verdict: protoVerdict(verdict),
    expectedVersion: BigInt(expectedVersion),
    idempotencyKey: crypto.randomUUID(),
  });
  const response = fromJson(
    DecideActionRequestResponseSchema,
    await readJson(
      `${ACTIONS_PATH}/v1/operator/action-requests/${encodeURIComponent(requestId)}/decision`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(toJson(DecideActionRequestRequestSchema, decision)),
      },
      fetcher
    )
  );
  if (response.actionRequest === undefined) throw new Error("The Action Service returned no decided request.");
  return actionRequestFromMessage(response.actionRequest);
}

export function decideAction(request: ActionRequestView, verdict: Verdict): Promise<ActionRequestView> {
  return decideActionById(request.id, request.version, verdict);
}

export type ActionRequestWatchFrame =
  | { kind: "snapshot"; requests: ActionRequestView[] }
  | { kind: "snapshot_complete" }
  | { kind: "upsert"; request: ActionRequestView }
  | { kind: "reconnect_required" };

export type ActionGroupWatchFrame =
  | { kind: "snapshot"; groups: ActionGroupView[] }
  | { kind: "snapshot_complete" }
  | { kind: "upsert"; group: ActionGroupView }
  | { kind: "reconnect_required" };

export function followActionRequests(handlers: {
  onOpen: () => void;
  onFrame: (frame: ActionRequestWatchFrame) => void;
  onConnection: (connection: StreamConnection) => void;
}): () => void {
  const query = new URLSearchParams({ state: actionStateJson(ProtoActionState.DECISION_PENDING) });
  return followWatch(`${ACTIONS_PATH}/v1/action-requests:watch?${query}`, {
    ...handlers,
    onFrame: (result) => {
      const message = fromJson(WatchActionRequestsResponseSchema, protoJson(result));
      switch (message.frame.case) {
        case "snapshot":
          handlers.onFrame({
            kind: "snapshot",
            requests: message.frame.value.actionRequests.map(actionRequestFromMessage),
          });
          break;
        case "snapshotComplete":
          handlers.onFrame({ kind: "snapshot_complete" });
          break;
        case "upsert":
          handlers.onFrame({ kind: "upsert", request: actionRequestFromMessage(message.frame.value) });
          break;
        case "reconnectRequired":
          handlers.onFrame({ kind: "reconnect_required" });
          break;
        case undefined:
          throw new Error("The Action Service returned an empty request watch frame.");
      }
    },
  });
}

export function followActionGroups(handlers: {
  onOpen: () => void;
  onFrame: (frame: ActionGroupWatchFrame) => void;
  onConnection: (connection: StreamConnection) => void;
}): () => void {
  return followWatch(`${ACTIONS_PATH}/v1/action-groups:watch`, {
    ...handlers,
    onFrame: (result) => {
      const message = fromJson(WatchActionGroupsResponseSchema, protoJson(result));
      switch (message.frame.case) {
        case "snapshot":
          handlers.onFrame({
            kind: "snapshot",
            groups: message.frame.value.actionGroups.map(actionGroupFromMessage),
          });
          break;
        case "snapshotComplete":
          handlers.onFrame({ kind: "snapshot_complete" });
          break;
        case "upsert":
          handlers.onFrame({ kind: "upsert", group: actionGroupFromMessage(message.frame.value) });
          break;
        case "reconnectRequired":
          handlers.onFrame({ kind: "reconnect_required" });
          break;
        case undefined:
          throw new Error("The Action Service returned an empty group watch frame.");
      }
    },
  });
}
