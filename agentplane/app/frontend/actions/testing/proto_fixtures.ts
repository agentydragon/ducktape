import { create, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import { TimestampSchema } from "@bufbuild/protobuf/wkt";

import {
  ActionState,
  GetActionRequestResponseSchema,
  ListActionGroupsResponseSchema,
  ListActionRequestHistoryResponseSchema,
  ListActionRequestsRequestSchema,
  ListActionRequestsResponseSchema,
  WatchActionGroupsResponseSchema,
  WatchActionRequestsResponseSchema,
  type ActionGroup,
  type ActionRequest,
  type ListActionRequestHistoryResponse,
} from "../../../../action_service/action_service_pb";
import {
  DecideActionRequestResponseSchema,
  DisconnectMcpServerResponseSchema,
  McpLinkageStatus,
  McpRefreshAction,
  McpServerLinkageSchema,
  StartMcpServerLinkageResponseSchema,
  WatchMcpServersResponseSchema,
} from "../../../../action_service/operator_action_service_pb";
import type { McpLinkageView } from "../../mcp_linkage_client";

export function actionStateProtoJson(value: ActionState): string {
  const json = toJson(ListActionRequestsRequestSchema, create(ListActionRequestsRequestSchema, { state: [value] }));
  if (json === null || Array.isArray(json) || typeof json !== "object") {
    throw new Error("Could not encode the Action request state.");
  }
  const states = json.state;
  if (!Array.isArray(states) || typeof states[0] !== "string") {
    throw new Error("Could not encode the Action request state.");
  }
  return states[0];
}

export function listActionRequestsProtoJson(values: readonly ActionRequest[]): JsonValue {
  return toJson(
    ListActionRequestsResponseSchema,
    create(ListActionRequestsResponseSchema, { actionRequests: [...values] })
  );
}

export function listActionHistoryProtoJson(value: ListActionRequestHistoryResponse): JsonValue {
  return toJson(ListActionRequestHistoryResponseSchema, value);
}

export function getActionRequestProtoJson(value: ActionRequest): JsonValue {
  return toJson(GetActionRequestResponseSchema, create(GetActionRequestResponseSchema, { actionRequest: value }));
}

export function decideActionRequestProtoJson(value: ActionRequest): JsonValue {
  return toJson(DecideActionRequestResponseSchema, create(DecideActionRequestResponseSchema, { actionRequest: value }));
}

export function listActionGroupsProtoJson(values: readonly ActionGroup[]): JsonValue {
  return toJson(ListActionGroupsResponseSchema, create(ListActionGroupsResponseSchema, { actionGroups: [...values] }));
}

export function actionRequestsSnapshotProtoJson(values: readonly ActionRequest[]): JsonValue {
  return toJson(
    WatchActionRequestsResponseSchema,
    create(WatchActionRequestsResponseSchema, {
      frame: { case: "snapshot", value: { actionRequests: [...values] } },
    })
  );
}

export function actionRequestUpsertProtoJson(value: ActionRequest): JsonValue {
  return toJson(
    WatchActionRequestsResponseSchema,
    create(WatchActionRequestsResponseSchema, {
      frame: { case: "upsert", value },
    })
  );
}

export function actionRequestsSnapshotCompleteProtoJson(): JsonValue {
  return toJson(
    WatchActionRequestsResponseSchema,
    create(WatchActionRequestsResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
  );
}

export function actionGroupsSnapshotProtoJson(values: readonly ActionGroup[]): JsonValue {
  return toJson(
    WatchActionGroupsResponseSchema,
    create(WatchActionGroupsResponseSchema, {
      frame: { case: "snapshot", value: { actionGroups: [...values] } },
    })
  );
}

export function actionGroupUpsertProtoJson(value: ActionGroup): JsonValue {
  return toJson(
    WatchActionGroupsResponseSchema,
    create(WatchActionGroupsResponseSchema, {
      frame: { case: "upsert", value },
    })
  );
}

export function actionGroupsSnapshotCompleteProtoJson(): JsonValue {
  return toJson(
    WatchActionGroupsResponseSchema,
    create(WatchActionGroupsResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
  );
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

function timestamp(value: string) {
  return fromJson(TimestampSchema, value);
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

export function mcpLinkagesSnapshotProtoJson(values: readonly McpLinkageView[]): JsonValue {
  return toJson(
    WatchMcpServersResponseSchema,
    create(WatchMcpServersResponseSchema, {
      frame: { case: "snapshot", value: { linkages: values.map(mcpLinkageMessage) } },
    })
  );
}

export function mcpLinkageUpsertProtoJson(value: McpLinkageView): JsonValue {
  return toJson(
    WatchMcpServersResponseSchema,
    create(WatchMcpServersResponseSchema, { frame: { case: "upsert", value: mcpLinkageMessage(value) } })
  );
}

export function mcpLinkagesSnapshotCompleteProtoJson(): JsonValue {
  return toJson(
    WatchMcpServersResponseSchema,
    create(WatchMcpServersResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
  );
}

export function startMcpLinkageProtoJson(authorizationUrl: string): JsonValue {
  return toJson(StartMcpServerLinkageResponseSchema, create(StartMcpServerLinkageResponseSchema, { authorizationUrl }));
}

export function disconnectMcpLinkageProtoJson(value: McpLinkageView): JsonValue {
  return toJson(
    DisconnectMcpServerResponseSchema,
    create(DisconnectMcpServerResponseSchema, { linkage: mcpLinkageMessage(value) })
  );
}
