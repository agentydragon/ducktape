import { create, toJson, type JsonValue } from "@bufbuild/protobuf";

import {
  type ActionState,
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
  StartMcpServerLinkageResponseSchema,
  WatchMcpServersResponseSchema,
  type McpServerLinkage,
} from "../../../../action_service/operator_action_service_pb";

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

export function mcpLinkagesSnapshotProtoJson(values: readonly McpServerLinkage[]): JsonValue {
  return toJson(
    WatchMcpServersResponseSchema,
    create(WatchMcpServersResponseSchema, {
      frame: { case: "snapshot", value: { linkages: [...values] } },
    })
  );
}

export function mcpLinkageUpsertProtoJson(value: McpServerLinkage): JsonValue {
  return toJson(
    WatchMcpServersResponseSchema,
    create(WatchMcpServersResponseSchema, { frame: { case: "upsert", value } })
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

export function disconnectMcpLinkageProtoJson(value: McpServerLinkage): JsonValue {
  return toJson(DisconnectMcpServerResponseSchema, create(DisconnectMcpServerResponseSchema, { linkage: value }));
}
