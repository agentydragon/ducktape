import { toJson, type JsonObject, type JsonValue } from "@bufbuild/protobuf";
import { TimestampSchema, ValueSchema, type Timestamp } from "@bufbuild/protobuf/wkt";

import {
  ExecutionErrorSchema,
  McpLifecycle,
  McpUnavailableReason,
  type ActionIdentity,
  type ActionRequest,
  type Execution,
} from "../../../action_service/action_service_pb";

/** The tool renderers and notification text consume the argument JSON the caller submitted. */
export function actionArguments(request: ActionRequest): JsonObject {
  return request.arguments ?? {};
}

/** MCP executions keep the complete CallToolResult in their protobuf Value. */
export function executionResult(execution: Execution | undefined): JsonValue | undefined {
  return execution?.result === undefined ? undefined : toJson(ValueSchema, execution.result);
}

/** The typed error as its ProtoJSON, which the history view shows as is. */
export function executionError(execution: Execution | undefined): JsonValue | undefined {
  return execution?.error === undefined ? undefined : toJson(ExecutionErrorSchema, execution.error);
}

export function actionIdentity(request: ActionRequest): ActionIdentity {
  if (request.action === undefined) throw new Error("The Action Service omitted ActionRequest.action.");
  return request.action;
}

export function actionCaller(request: ActionRequest): NonNullable<ActionRequest["caller"]> {
  if (request.caller === undefined) throw new Error("The Action Service omitted ActionRequest.caller.");
  return request.caller;
}

export function timestampText(value: Timestamp | undefined): string | undefined {
  if (value === undefined) return undefined;
  const json = toJson(TimestampSchema, value);
  if (typeof json !== "string") throw new Error("The Action Service returned an invalid timestamp.");
  return json;
}

export function mcpLifecycleLabel(value: McpLifecycle): string {
  switch (value) {
    case McpLifecycle.DISCONNECTED:
      return "disconnected";
    case McpLifecycle.CONNECTING:
      return "connecting";
    case McpLifecycle.DISCOVERING:
      return "discovering";
    case McpLifecycle.AVAILABLE:
      return "available";
    case McpLifecycle.DRAINING:
      return "draining";
    case McpLifecycle.STOPPED:
      return "stopped";
    default:
      throw new Error(`Unknown MCP lifecycle state (${value}).`);
  }
}

export function mcpUnavailableReasonLabel(value: McpUnavailableReason | undefined): string | null {
  switch (value) {
    case undefined:
    case McpUnavailableReason.UNSPECIFIED:
      return null;
    case McpUnavailableReason.CONNECT_FAILED:
      return "connect_failed";
    case McpUnavailableReason.DISCOVERY_FAILED:
      return "discovery_failed";
    case McpUnavailableReason.INVALID_CATALOG:
      return "invalid_catalog";
    case McpUnavailableReason.LINKAGE_UNAVAILABLE:
      return "linkage_unavailable";
    case McpUnavailableReason.SESSION_FAILED:
      return "session_failed";
    case McpUnavailableReason.SUPERVISOR_STOPPED:
      return "supervisor_stopped";
    default:
      throw new Error(`Unknown MCP unavailable reason (${value}).`);
  }
}
