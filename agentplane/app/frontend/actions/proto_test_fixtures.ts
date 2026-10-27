import { create, fromJson, toJson, type JsonValue as ProtoJsonValue } from "@bufbuild/protobuf";
import { TimestampSchema, ValueSchema, type Timestamp } from "@bufbuild/protobuf/wkt";

import {
  ActionState as ProtoActionState,
  ExecutorKind as ProtoExecutorKind,
  ExecutionState as ProtoExecutionState,
  McpLifecycle as ProtoMcpLifecycle,
  McpUnavailableReason as ProtoMcpUnavailableReason,
  PolicyKind as ProtoPolicyKind,
  PolicySource as ProtoPolicySource,
  Verdict as ProtoVerdict,
  ActionGroupSchema,
  ActionRequestSchema,
  BindingEvidenceSchema,
  GetActionRequestResponseSchema,
  ListActionGroupsResponseSchema,
  ListActionRequestHistoryResponseSchema,
  ListActionRequestsRequestSchema,
  ListActionRequestsResponseSchema,
  MatchedPolicySchema,
  MatchedRepositorySchema,
  PolicyEvidenceSchema,
  PolicySetEvidenceSchema,
  WatchActionGroupsResponseSchema,
  WatchActionRequestsResponseSchema,
  type ActionGroup,
  type ActionRequest,
} from "../../../action_service/action_service_pb";
import {
  DecideActionRequestResponseSchema,
  DisconnectMcpServerResponseSchema,
  McpLinkageStatus,
  McpRefreshAction,
  McpServerLinkageSchema,
  StartMcpServerLinkageResponseSchema,
  WatchMcpServersResponseSchema,
} from "../../../action_service/operator_action_service_pb";
import type { McpLinkageView } from "../mcp_linkage_client";
import type {
  ActionGroupView,
  ActionHistoryPage,
  ActionRequestView,
  ActionState,
  ExecutionState,
  PolicyEvidence,
} from "./types";

function actionState(value: ActionState): ProtoActionState {
  switch (value) {
    case "decision_pending":
      return ProtoActionState.DECISION_PENDING;
    case "allowed":
      return ProtoActionState.ALLOWED;
    case "denied":
      return ProtoActionState.DENIED;
    case "dispatching":
      return ProtoActionState.DISPATCHING;
    case "running":
      return ProtoActionState.RUNNING;
    case "succeeded":
      return ProtoActionState.SUCCEEDED;
    case "failed":
      return ProtoActionState.FAILED;
    case "cancelled":
      return ProtoActionState.CANCELLED;
    case "execution_unknown":
      return ProtoActionState.EXECUTION_UNKNOWN;
    default:
      throw new Error(`Unknown fixture Action state: ${value}`);
  }
}

function executionState(value: ExecutionState): ProtoExecutionState {
  switch (value) {
    case "pending_dispatch":
      return ProtoExecutionState.PENDING_DISPATCH;
    case "dispatching":
      return ProtoExecutionState.DISPATCHING;
    case "running":
      return ProtoExecutionState.RUNNING;
    case "succeeded":
      return ProtoExecutionState.SUCCEEDED;
    case "failed":
      return ProtoExecutionState.FAILED;
    case "cancelled":
      return ProtoExecutionState.CANCELLED;
    case "execution_unknown":
      return ProtoExecutionState.EXECUTION_UNKNOWN;
    default:
      throw new Error(`Unknown fixture execution state: ${value}`);
  }
}

function verdict(value: "allow" | "deny"): ProtoVerdict {
  switch (value) {
    case "allow":
      return ProtoVerdict.ALLOW;
    case "deny":
      return ProtoVerdict.DENY;
    default:
      throw new Error(`Unknown fixture verdict: ${value}`);
  }
}

function policySource(value: string): ProtoPolicySource {
  switch (value) {
    case "rules":
      return ProtoPolicySource.RULES;
    default:
      throw new Error(`Unknown fixture policy source: ${value}`);
  }
}

function policyKind(value: string): ProtoPolicyKind {
  switch (value) {
    case "exact_actions":
      return ProtoPolicyKind.EXACT_ACTIONS;
    case "argument_schema":
      return ProtoPolicyKind.ARGUMENT_SCHEMA;
    case "github_repository":
      return ProtoPolicyKind.GITHUB_REPOSITORY;
    case "github_public_repository":
      return ProtoPolicyKind.GITHUB_PUBLIC_REPOSITORY;
    case "home_assistant_entity_control":
      return ProtoPolicyKind.HOME_ASSISTANT_ENTITY_CONTROL;
    default:
      throw new Error(`Unknown fixture policy kind: ${value}`);
  }
}

function executorKind(value: ActionGroupView["executor_kind"]): ProtoExecutorKind {
  switch (value) {
    case "mcp":
      return ProtoExecutorKind.MCP;
    case "pod":
      return ProtoExecutorKind.POD;
    case "orchestration_v1":
      return ProtoExecutorKind.ORCHESTRATION_V1;
    default:
      throw new Error(`Unknown fixture executor kind: ${value}`);
  }
}

function mcpLifecycle(value: NonNullable<ActionGroupView["health"]>["state"]): ProtoMcpLifecycle {
  switch (value) {
    case "disconnected":
      return ProtoMcpLifecycle.DISCONNECTED;
    case "connecting":
      return ProtoMcpLifecycle.CONNECTING;
    case "discovering":
      return ProtoMcpLifecycle.DISCOVERING;
    case "available":
      return ProtoMcpLifecycle.AVAILABLE;
    case "draining":
      return ProtoMcpLifecycle.DRAINING;
    case "stopped":
      return ProtoMcpLifecycle.STOPPED;
    default:
      throw new Error(`Unknown fixture MCP lifecycle state: ${value}`);
  }
}

function mcpUnavailableReason(
  value: NonNullable<ActionGroupView["health"]>["reason"]
): ProtoMcpUnavailableReason | undefined {
  if (value === null) return undefined;
  switch (value) {
    case "connect_failed":
      return ProtoMcpUnavailableReason.CONNECT_FAILED;
    case "discovery_failed":
      return ProtoMcpUnavailableReason.DISCOVERY_FAILED;
    case "invalid_catalog":
      return ProtoMcpUnavailableReason.INVALID_CATALOG;
    case "linkage_unavailable":
      return ProtoMcpUnavailableReason.LINKAGE_UNAVAILABLE;
    case "session_failed":
      return ProtoMcpUnavailableReason.SESSION_FAILED;
    case "supervisor_stopped":
      return ProtoMcpUnavailableReason.SUPERVISOR_STOPPED;
    default:
      throw new Error(`Unknown fixture MCP unavailable reason: ${value}`);
  }
}

function mcpLinkageStatus(value: McpLinkageView["status"]): McpLinkageStatus {
  switch (value) {
    case "unlinked":
      return McpLinkageStatus.UNLINKED;
    case "linked":
      return McpLinkageStatus.LINKED;
    case "degraded":
      return McpLinkageStatus.DEGRADED;
    case "expired":
      return McpLinkageStatus.EXPIRED;
    default:
      throw new Error(`Unknown fixture MCP linkage status: ${value}`);
  }
}

function mcpRefreshAction(value: NonNullable<McpLinkageView["refresh_failure"]>["action"]): McpRefreshAction {
  switch (value) {
    case "reconnect":
      return McpRefreshAction.RECONNECT;
    case "retrying":
      return McpRefreshAction.RETRYING;
    default:
      throw new Error(`Unknown fixture MCP refresh action: ${value}`);
  }
}

function timestamp(value: string): Timestamp {
  return fromJson(TimestampSchema, value);
}

function safeInt64(value: number, field: string): bigint {
  if (!Number.isSafeInteger(value)) throw new Error(`Fixture ${field} is not a safe integer.`);
  return BigInt(value);
}

function policyEvidence(value: PolicyEvidence): NonNullable<NonNullable<ActionRequest["decision"]>["policyEvidence"]> {
  return create(PolicyEvidenceSchema, {
    bindings: value.bindings.map((binding) =>
      create(BindingEvidenceSchema, {
        namespace: binding.namespace,
        name: binding.name,
        resourceVersion: binding.resource_version,
      })
    ),
    policySets: value.policy_sets.map((policySet) =>
      create(PolicySetEvidenceSchema, {
        namespace: policySet.namespace,
        name: policySet.name,
        generation: safeInt64(policySet.generation, "policy generation"),
      })
    ),
    matched: create(MatchedPolicySchema, {
      namespace: value.matched.namespace,
      policySet: value.matched.policy_set,
      source: policySource(value.matched.source),
      index: value.matched.index,
      type: policyKind(value.matched.type),
      repository:
        value.matched.repository === null
          ? undefined
          : create(MatchedRepositorySchema, {
              owner: value.matched.repository.owner,
              repository: value.matched.repository.repository,
              confirmedPublic: value.matched.repository.confirmed_public,
            }),
    }),
  });
}

function actionRequestMessage(value: ActionRequestView): ActionRequest {
  return create(ActionRequestSchema, {
    id: value.id,
    idempotencyKey: value.idempotency_key,
    action: { group: value.action.group, name: value.action.name },
    arguments: value.arguments,
    title: value.title,
    description: value.description ?? undefined,
    caller: { namespace: value.caller.namespace, name: value.caller.name },
    state: actionState(value.state),
    version: safeInt64(value.version, "ActionRequest.version"),
    createdAt: timestamp(value.created_at),
    updatedAt: timestamp(value.updated_at),
    decision:
      value.decision === null
        ? undefined
        : {
            id: value.decision.id,
            verdict: verdict(value.decision.verdict),
            provider: value.decision.provider,
            operator:
              value.decision.operator === null
                ? undefined
                : { issuer: value.decision.operator.issuer, subject: value.decision.operator.subject },
            decisionNote: value.decision.decision_note ?? undefined,
            reasonCode: value.decision.reason_code ?? undefined,
            reasonDescription: value.decision.reason_description ?? undefined,
            policyEvidence:
              value.decision.policy_evidence === null ? undefined : policyEvidence(value.decision.policy_evidence),
            idempotencyKey: value.decision.idempotency_key,
            decidedAt: timestamp(value.decision.decided_at),
          },
    execution:
      value.execution === null
        ? undefined
        : {
            id: value.execution.id,
            state: executionState(value.execution.state),
            result:
              value.execution.result === null && value.execution.state !== "succeeded"
                ? undefined
                : fromJson(ValueSchema, value.execution.result),
            error: value.execution.error === null ? undefined : value.execution.error,
            createdAt: timestamp(value.execution.created_at),
            startedAt: value.execution.started_at === null ? undefined : timestamp(value.execution.started_at),
            reconciledAt: value.execution.reconciled_at === null ? undefined : timestamp(value.execution.reconciled_at),
            completedAt: value.execution.completed_at === null ? undefined : timestamp(value.execution.completed_at),
          },
  });
}

function actionGroupMessage(value: ActionGroupView): ActionGroup {
  return create(ActionGroupSchema, {
    key: value.key,
    title: value.title,
    description: value.description,
    executorKind: executorKind(value.executor_kind),
    executorDescription: value.executor_description,
    available: value.available,
    health:
      value.health === null
        ? undefined
        : {
            state: mcpLifecycle(value.health.state),
            reason: mcpUnavailableReason(value.health.reason),
            detail: value.health.detail ?? undefined,
            lastDiscoveryAt:
              value.health.last_discovery_at === null ? undefined : timestamp(value.health.last_discovery_at),
            retryAt: value.health.retry_at === null ? undefined : timestamp(value.health.retry_at),
            failures: value.health.failures,
          },
    actions: value.actions.map((action) => ({
      group: action.group,
      name: action.name,
      description: action.description,
      inputSchema: action.input_schema,
      title: action.title ?? undefined,
      annotations:
        action.annotations === null
          ? undefined
          : {
              readOnlyHint: action.annotations.read_only_hint ?? undefined,
              destructiveHint: action.annotations.destructive_hint ?? undefined,
              idempotentHint: action.annotations.idempotent_hint ?? undefined,
              openWorldHint: action.annotations.open_world_hint ?? undefined,
            },
    })),
    directTools: value.direct_tools,
  });
}

function mcpLinkageMessage(value: McpLinkageView) {
  return create(McpServerLinkageSchema, {
    serverId: value.server_id,
    serverUrl: value.server_url,
    status: mcpLinkageStatus(value.status),
    scopes: value.scopes,
    refreshFailure:
      value.refresh_failure === null
        ? undefined
        : {
            action: mcpRefreshAction(value.refresh_failure.action),
            error: value.refresh_failure.error,
            attempts: value.refresh_failure.attempts,
            retryAt: value.refresh_failure.retry_at === null ? undefined : timestamp(value.refresh_failure.retry_at),
          },
  });
}

export function actionStateProtoJson(value: ActionState): string {
  const json = toJson(
    ListActionRequestsRequestSchema,
    create(ListActionRequestsRequestSchema, { state: [actionState(value)] })
  );
  if (json === null || Array.isArray(json) || typeof json !== "object") {
    throw new Error("Could not encode the Action request state.");
  }
  const states = json.state;
  if (!Array.isArray(states) || typeof states[0] !== "string") {
    throw new Error("Could not encode the Action request state.");
  }
  return states[0];
}

export function listActionRequestsProtoJson(values: readonly ActionRequestView[]): ProtoJsonValue {
  return toJson(
    ListActionRequestsResponseSchema,
    create(ListActionRequestsResponseSchema, { actionRequests: values.map(actionRequestMessage) })
  );
}

export function listActionHistoryProtoJson(value: ActionHistoryPage): ProtoJsonValue {
  return toJson(
    ListActionRequestHistoryResponseSchema,
    create(ListActionRequestHistoryResponseSchema, {
      actionRequests: value.items.map(actionRequestMessage),
      nextCursor: value.next_cursor ?? undefined,
    })
  );
}

export function getActionRequestProtoJson(value: ActionRequestView): ProtoJsonValue {
  return toJson(
    GetActionRequestResponseSchema,
    create(GetActionRequestResponseSchema, { actionRequest: actionRequestMessage(value) })
  );
}

export function decideActionRequestProtoJson(value: ActionRequestView): ProtoJsonValue {
  return toJson(
    DecideActionRequestResponseSchema,
    create(DecideActionRequestResponseSchema, { actionRequest: actionRequestMessage(value) })
  );
}

export function listActionGroupsProtoJson(values: readonly ActionGroupView[]): ProtoJsonValue {
  return toJson(
    ListActionGroupsResponseSchema,
    create(ListActionGroupsResponseSchema, { actionGroups: values.map(actionGroupMessage) })
  );
}

export function actionRequestsSnapshotProtoJson(values: readonly ActionRequestView[]): ProtoJsonValue {
  return toJson(
    WatchActionRequestsResponseSchema,
    create(WatchActionRequestsResponseSchema, {
      frame: { case: "snapshot", value: { actionRequests: values.map(actionRequestMessage) } },
    })
  );
}

export function actionRequestUpsertProtoJson(value: ActionRequestView): ProtoJsonValue {
  return toJson(
    WatchActionRequestsResponseSchema,
    create(WatchActionRequestsResponseSchema, { frame: { case: "upsert", value: actionRequestMessage(value) } })
  );
}

export function actionRequestsSnapshotCompleteProtoJson(): ProtoJsonValue {
  return toJson(
    WatchActionRequestsResponseSchema,
    create(WatchActionRequestsResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
  );
}

export function actionGroupsSnapshotProtoJson(values: readonly ActionGroupView[]): ProtoJsonValue {
  return toJson(
    WatchActionGroupsResponseSchema,
    create(WatchActionGroupsResponseSchema, {
      frame: { case: "snapshot", value: { actionGroups: values.map(actionGroupMessage) } },
    })
  );
}

export function actionGroupUpsertProtoJson(value: ActionGroupView): ProtoJsonValue {
  return toJson(
    WatchActionGroupsResponseSchema,
    create(WatchActionGroupsResponseSchema, { frame: { case: "upsert", value: actionGroupMessage(value) } })
  );
}

export function actionGroupsSnapshotCompleteProtoJson(): ProtoJsonValue {
  return toJson(
    WatchActionGroupsResponseSchema,
    create(WatchActionGroupsResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
  );
}

export function mcpLinkagesSnapshotProtoJson(values: readonly McpLinkageView[]): ProtoJsonValue {
  return toJson(
    WatchMcpServersResponseSchema,
    create(WatchMcpServersResponseSchema, {
      frame: { case: "snapshot", value: { linkages: values.map(mcpLinkageMessage) } },
    })
  );
}

export function mcpLinkageUpsertProtoJson(value: McpLinkageView): ProtoJsonValue {
  return toJson(
    WatchMcpServersResponseSchema,
    create(WatchMcpServersResponseSchema, { frame: { case: "upsert", value: mcpLinkageMessage(value) } })
  );
}

export function mcpLinkagesSnapshotCompleteProtoJson(): ProtoJsonValue {
  return toJson(
    WatchMcpServersResponseSchema,
    create(WatchMcpServersResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
  );
}

export function startMcpLinkageProtoJson(authorizationUrl: string): ProtoJsonValue {
  return toJson(StartMcpServerLinkageResponseSchema, create(StartMcpServerLinkageResponseSchema, { authorizationUrl }));
}

export function disconnectMcpLinkageProtoJson(value: McpLinkageView): ProtoJsonValue {
  return toJson(
    DisconnectMcpServerResponseSchema,
    create(DisconnectMcpServerResponseSchema, { linkage: mcpLinkageMessage(value) })
  );
}
