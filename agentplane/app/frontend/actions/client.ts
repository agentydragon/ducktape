import { create, fromJson, fromJsonString, toJson } from "@bufbuild/protobuf";

import { fetchText, fetchWithLogin, type Fetcher } from "../transport/client";
import type { StreamConnection } from "../transport/stream_reader";
import { followWatch } from "../transport/watch_stream";
import {
  ActionState,
  Verdict,
  GetActionRequestResponseSchema,
  ListActionGroupsResponseSchema,
  ListActionRequestHistoryResponseSchema,
  ListActionRequestsRequestSchema,
  ListActionRequestsResponseSchema,
  WatchActionGroupsResponseSchema,
  WatchActionRequestsResponseSchema,
  type WatchActionGroupsResponse,
  type WatchActionRequestsResponse,
  type ActionGroup,
  type ActionRequest,
  type Decision,
  type ListActionRequestHistoryResponse,
} from "../../../action_service/api/contracts/action_service_pb";
import {
  DecideActionRequestRequestSchema,
  DecideActionRequestResponseSchema,
  GetGmailResourceSummariesResponseSchema,
  type GetGmailResourceSummariesResponse,
  GetGmailThreadSummariesResponseSchema,
  GetGoogleCalendarEventPreviewResponseSchema,
  type GetGoogleCalendarEventPreviewResponse,
  GetGrocyReferenceSummariesResponseSchema,
  type GrocyReferenceSummary,
  GetTanaNodeSummariesResponseSchema,
} from "../../../action_service/api/contracts/operator_action_service_pb";

const ACTIONS_PATH = "/upstream/actions";

function required<T>(value: T | undefined, field: string): T {
  if (value === undefined) throw new Error(`The Action Service omitted ${field}.`);
  return value;
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
      break;
    default:
      throw new Error("The Action Service omitted Decision.decider.");
  }
}

function validateActionRequest(value: ActionRequest): ActionRequest {
  required(value.action, "ActionRequest.action");
  if (value.caller !== undefined && value.callerIdentity !== undefined) {
    throw new Error("The Action Service returned an ActionRequest with multiple callers.");
  }
  if (value.callerIdentity !== undefined) {
    if (value.callerIdentity.name === "") {
      throw new Error("The Action Service returned an Identity caller without a name.");
    }
    if (value.callerIdentity.uid === "") {
      throw new Error("The Action Service returned an Identity caller without a UID.");
    }
  }
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
  if (value.execution !== undefined) required(value.execution.createdAt, "Execution.created_at");
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
  return response.actionGroups;
}

export async function getAction(requestId: string): Promise<ActionRequest> {
  const response = fromJsonString(
    GetActionRequestResponseSchema,
    await fetchText(`${ACTIONS_PATH}/v1/action-requests/${encodeURIComponent(requestId)}`)
  );
  return validateActionRequest(required(response.actionRequest, "request"));
}

/** Live display metadata for a saved receipt. These optional reads never redirect to login. */
export async function getIntegrationPreviewNames(
  kind: "gmail" | "tana",
  requestId: string,
  signal?: AbortSignal
): Promise<ReadonlyMap<string, string>> {
  const timeout = AbortSignal.timeout(3000);
  const path = `${ACTIONS_PATH}/v1/operator/action-requests/${encodeURIComponent(requestId)}`;
  const options = { signal: signal ? AbortSignal.any([signal, timeout]) : timeout, cache: "no-store" } as const;
  if (kind === "gmail") {
    const { threads } = fromJsonString(
      GetGmailThreadSummariesResponseSchema,
      await fetchText(`${path}/gmail-thread-summaries`, options, fetch)
    );
    return new Map(threads.map(({ threadId, subject }) => [threadId, subject]));
  }
  const { nodes, workspace } = fromJsonString(
    GetTanaNodeSummariesResponseSchema,
    await fetchText(`${path}/tana-node-summaries`, options, fetch)
  );
  const names = new Map(nodes.map(({ nodeId, name }) => [nodeId, name]));
  if (workspace !== undefined) names.set(workspace.workspaceId, workspace.name);
  return names;
}

export async function getGoogleCalendarPreview(
  requestId: string,
  signal: AbortSignal
): Promise<GetGoogleCalendarEventPreviewResponse> {
  const path = `${ACTIONS_PATH}/v1/operator/action-requests/${encodeURIComponent(requestId)}`;
  return fromJsonString(
    GetGoogleCalendarEventPreviewResponseSchema,
    await fetchText(
      `${path}/google-calendar-event-preview`,
      { signal: AbortSignal.any([signal, AbortSignal.timeout(3000)]), cache: "no-store" },
      fetch
    )
  );
}

export async function getGmailResourceSummaries(
  requestId: string,
  signal: AbortSignal
): Promise<GetGmailResourceSummariesResponse> {
  const path = `${ACTIONS_PATH}/v1/operator/action-requests/${encodeURIComponent(requestId)}/gmail-resource-summaries`;
  return fromJsonString(
    GetGmailResourceSummariesResponseSchema,
    await fetchText(path, { signal: AbortSignal.any([signal, AbortSignal.timeout(3000)]), cache: "no-store" }, fetch)
  );
}

export async function getGrocyReferenceSummaries(
  requestId: string,
  signal: AbortSignal
): Promise<ReadonlyMap<string, GrocyReferenceSummary>> {
  const timeout = AbortSignal.timeout(3000);
  const path = `${ACTIONS_PATH}/v1/operator/action-requests/${encodeURIComponent(requestId)}`;
  const options = { signal: AbortSignal.any([signal, timeout]), cache: "no-store" } as const;
  const { references } = fromJsonString(
    GetGrocyReferenceSummariesResponseSchema,
    await fetchText(`${path}/grocy-reference-summaries`, options, fetch)
  );
  return new Map(references.map((summary) => [summary.key, summary]));
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

export function followActionRequests(handlers: {
  onOpen: () => void;
  onFrame: (frame: WatchActionRequestsResponse) => void;
  onConnection: (connection: StreamConnection) => void;
}): () => void {
  const query = new URLSearchParams({ state: actionStateJson(ActionState.DECISION_PENDING) });
  return followWatch(`${ACTIONS_PATH}/v1/action-requests:watch?${query}`, {
    ...handlers,
    onFrame: (result) => {
      const message = fromJson(WatchActionRequestsResponseSchema, result);
      switch (message.frame.case) {
        case "snapshot":
          message.frame.value.actionRequests.forEach(validateActionRequest);
          break;
        case "upsert":
          validateActionRequest(message.frame.value);
          break;
        case undefined:
          throw new Error("The Action Service returned an empty request watch frame.");
      }
      handlers.onFrame(message);
    },
  });
}

export function followActionGroups(handlers: {
  onOpen: () => void;
  onFrame: (frame: WatchActionGroupsResponse) => void;
  onConnection: (connection: StreamConnection) => void;
}): () => void {
  return followWatch(`${ACTIONS_PATH}/v1/action-groups:watch`, {
    ...handlers,
    onFrame: (result) => {
      const message = fromJson(WatchActionGroupsResponseSchema, result);
      if (message.frame.case === undefined) throw new Error("The Action Service returned an empty group watch frame.");
      handlers.onFrame(message);
    },
  });
}
