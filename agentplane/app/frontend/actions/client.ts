import { create, fromJson, fromJsonString, toJson } from "@bufbuild/protobuf";

import { fetchText, fetchWithLogin, type Fetcher } from "../client";
import type { StreamConnection } from "../stream_reader";
import { followWatch } from "../watch_stream";
import {
  ActionState,
  ExecutorKind,
  ExecutionState,
  McpLifecycle,
  McpUnavailableReason,
  PolicyKind,
  PolicySource,
  Verdict,
  GetActionRequestResponseSchema,
  ListActionGroupsResponseSchema,
  ListActionRequestHistoryResponseSchema,
  ListActionRequestsRequestSchema,
  ListActionRequestsResponseSchema,
  WatchActionGroupsResponseSchema,
  WatchActionRequestsResponseSchema,
  type ActionGroup,
  type ActionRequest,
  type Decision,
  type Execution,
  type ListActionRequestHistoryResponse,
  type PolicyEvidence,
} from "../../../action_service/action_service_pb";
import {
  DecideActionRequestRequestSchema,
  DecideActionRequestResponseSchema,
} from "../../../action_service/operator_action_service_pb";

const ACTIONS_PATH = "/upstream/actions";

function required<T>(value: T | undefined, field: string): T {
  if (value === undefined) throw new Error(`The Action Service omitted ${field}.`);
  return value;
}

function validatePolicyEvidence(value: PolicyEvidence): void {
  const matched = required(value.matched, "policy evidence match");
  switch (matched.source) {
    case PolicySource.RULES:
      break;
    default:
      throw new Error(`The Action Service returned an unknown policy source (${matched.source}).`);
  }
  switch (matched.type) {
    case PolicyKind.EXACT_ACTIONS:
    case PolicyKind.ARGUMENT_SCHEMA:
    case PolicyKind.GITHUB_REPOSITORY:
    case PolicyKind.GITHUB_PUBLIC_REPOSITORY:
    case PolicyKind.HOME_ASSISTANT_ENTITY_CONTROL:
      break;
    default:
      throw new Error(`The Action Service returned an unknown policy kind (${matched.type}).`);
  }
}

function validateDecision(value: Decision): void {
  switch (value.verdict) {
    case Verdict.ALLOW:
    case Verdict.DENY:
      break;
    default:
      throw new Error(`The Action Service returned an unknown verdict (${value.verdict}).`);
  }
  required(value.decidedAt, "Decision.decided_at");
  switch (value.decider.case) {
    case "operator":
      required(value.decider.value.operator, "OperatorDecision.operator");
      break;
    case "policy":
      if (value.decider.value.evidence !== undefined) validatePolicyEvidence(value.decider.value.evidence);
      break;
    default:
      throw new Error("The Action Service omitted Decision.decider.");
  }
}

function validateExecution(value: Execution): void {
  switch (value.state) {
    case ExecutionState.PENDING_DISPATCH:
    case ExecutionState.DISPATCHING:
    case ExecutionState.RUNNING:
    case ExecutionState.SUCCEEDED:
    case ExecutionState.FAILED:
    case ExecutionState.CANCELLED:
    case ExecutionState.EXECUTION_UNKNOWN:
      break;
    default:
      throw new Error(`The Action Service returned an unknown execution state (${value.state}).`);
  }
  required(value.createdAt, "Execution.created_at");
}

function validateActionRequest(value: ActionRequest): ActionRequest {
  required(value.action, "ActionRequest.action");
  required(value.caller, "ActionRequest.caller");
  required(value.createdAt, "ActionRequest.created_at");
  required(value.updatedAt, "ActionRequest.updated_at");
  switch (value.state) {
    case ActionState.DECISION_PENDING:
    case ActionState.ALLOWED:
    case ActionState.DENIED:
    case ActionState.DISPATCHING:
    case ActionState.RUNNING:
    case ActionState.SUCCEEDED:
    case ActionState.FAILED:
    case ActionState.CANCELLED:
    case ActionState.EXECUTION_UNKNOWN:
      break;
    default:
      throw new Error(`The Action Service returned an unknown request state (${value.state}).`);
  }
  if (value.decision !== undefined) validateDecision(value.decision);
  if (value.execution !== undefined) validateExecution(value.execution);
  return value;
}

function validateActionGroup(value: ActionGroup): ActionGroup {
  switch (value.executorKind) {
    case ExecutorKind.MCP:
    case ExecutorKind.POD:
    case ExecutorKind.ORCHESTRATION_V1:
    case ExecutorKind.SESSION_DELEGATION:
      break;
    default:
      throw new Error(`The Action Service returned an unknown executor kind (${value.executorKind}).`);
  }
  if (value.health !== undefined) {
    switch (value.health.state) {
      case McpLifecycle.DISCONNECTED:
      case McpLifecycle.CONNECTING:
      case McpLifecycle.DISCOVERING:
      case McpLifecycle.AVAILABLE:
      case McpLifecycle.DRAINING:
      case McpLifecycle.STOPPED:
        break;
      default:
        throw new Error(`The Action Service returned an unknown MCP lifecycle state (${value.health.state}).`);
    }
    if (value.health.reason !== undefined) {
      switch (value.health.reason) {
        case McpUnavailableReason.UNSPECIFIED:
        case McpUnavailableReason.CONNECT_FAILED:
        case McpUnavailableReason.DISCOVERY_FAILED:
        case McpUnavailableReason.INVALID_CATALOG:
        case McpUnavailableReason.LINKAGE_UNAVAILABLE:
        case McpUnavailableReason.SESSION_FAILED:
        case McpUnavailableReason.SUPERVISOR_STOPPED:
          break;
        default:
          throw new Error(`The Action Service returned an unknown MCP unavailable reason (${value.health.reason}).`);
      }
    }
  }
  return value;
}

function actionStateJson(value: ActionState): string {
  const json = toJson(ListActionRequestsRequestSchema, create(ListActionRequestsRequestSchema, { state: [value] }));
  if (json === null || Array.isArray(json) || typeof json !== "object") {
    throw new Error("Could not encode the Action state.");
  }
  const states = json.state;
  if (!Array.isArray(states) || typeof states[0] !== "string") throw new Error("Could not encode the Action state.");
  return states[0];
}

export async function listActionRequests(
  state?: ActionState,
  fetcher: Fetcher = fetchWithLogin
): Promise<ActionRequest[]> {
  const query = new URLSearchParams();
  if (state !== undefined) query.set("state", actionStateJson(state));
  const response = fromJsonString(
    ListActionRequestsResponseSchema,
    await fetchText(`${ACTIONS_PATH}/v1/action-requests${query.size ? `?${query}` : ""}`, {}, fetcher)
  );
  return response.actionRequests.map(validateActionRequest);
}

export async function listActionGroups(): Promise<ActionGroup[]> {
  const response = fromJsonString(ListActionGroupsResponseSchema, await fetchText(`${ACTIONS_PATH}/v1/action-groups`));
  return response.actionGroups.map(validateActionGroup);
}

export async function getAction(requestId: string): Promise<ActionRequest> {
  const response = fromJsonString(
    GetActionRequestResponseSchema,
    await fetchText(`${ACTIONS_PATH}/v1/action-requests/${encodeURIComponent(requestId)}`)
  );
  return validateActionRequest(required(response.actionRequest, "request"));
}

export async function actionHistory(
  cursor?: string,
  includeAutoAllowed = true
): Promise<ListActionRequestHistoryResponse> {
  const query = new URLSearchParams({ limit: "50", includeAutoAllowed: String(includeAutoAllowed) });
  if (cursor !== undefined) query.set("cursor", cursor);
  const response = fromJsonString(
    ListActionRequestHistoryResponseSchema,
    await fetchText(`${ACTIONS_PATH}/v1/action-requests:history?${query}`)
  );
  response.actionRequests.forEach(validateActionRequest);
  return response;
}

export async function decideActionById(
  requestId: string,
  expectedVersion: bigint,
  verdict: Verdict,
  fetcher: Fetcher = fetchWithLogin
): Promise<ActionRequest> {
  if (expectedVersion < 1n) throw new Error("The reviewed Action version must be positive.");
  const decision = create(DecideActionRequestRequestSchema, {
    verdict,
    expectedVersion,
    idempotencyKey: crypto.randomUUID(),
  });
  const response = fromJsonString(
    DecideActionRequestResponseSchema,
    await fetchText(
      `${ACTIONS_PATH}/v1/operator/action-requests/${encodeURIComponent(requestId)}/decision`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(toJson(DecideActionRequestRequestSchema, decision)),
      },
      fetcher
    )
  );
  return validateActionRequest(required(response.actionRequest, "decided request"));
}

export function decideAction(request: ActionRequest, verdict: Verdict): Promise<ActionRequest> {
  return decideActionById(request.id, request.version, verdict);
}

export type ActionRequestWatchFrame =
  | { kind: "snapshot"; requests: ActionRequest[] }
  | { kind: "snapshot_complete" }
  | { kind: "upsert"; request: ActionRequest }
  | { kind: "reconnect_required" };

export type ActionGroupWatchFrame =
  | { kind: "snapshot"; groups: ActionGroup[] }
  | { kind: "snapshot_complete" }
  | { kind: "upsert"; group: ActionGroup }
  | { kind: "reconnect_required" };

export function followActionRequests(handlers: {
  onOpen: () => void;
  onFrame: (frame: ActionRequestWatchFrame) => void;
  onConnection: (connection: StreamConnection) => void;
}): () => void {
  const query = new URLSearchParams({ state: actionStateJson(ActionState.DECISION_PENDING) });
  return followWatch(`${ACTIONS_PATH}/v1/action-requests:watch?${query}`, {
    ...handlers,
    onFrame: (result) => {
      const message = fromJson(WatchActionRequestsResponseSchema, result);
      switch (message.frame.case) {
        case "snapshot":
          handlers.onFrame({
            kind: "snapshot",
            requests: message.frame.value.actionRequests.map(validateActionRequest),
          });
          break;
        case "snapshotComplete":
          handlers.onFrame({ kind: "snapshot_complete" });
          break;
        case "upsert":
          handlers.onFrame({ kind: "upsert", request: validateActionRequest(message.frame.value) });
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
      const message = fromJson(WatchActionGroupsResponseSchema, result);
      switch (message.frame.case) {
        case "snapshot":
          handlers.onFrame({ kind: "snapshot", groups: message.frame.value.actionGroups.map(validateActionGroup) });
          break;
        case "snapshotComplete":
          handlers.onFrame({ kind: "snapshot_complete" });
          break;
        case "upsert":
          handlers.onFrame({ kind: "upsert", group: validateActionGroup(message.frame.value) });
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
