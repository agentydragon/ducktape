import { create, fromJson, toJson, type JsonValue as ProtoJsonValue } from "@bufbuild/protobuf";
import { TimestampSchema, type Timestamp } from "@bufbuild/protobuf/wkt";

import { fetchWithLogin, HttpError } from "./client";
import type { StreamConnection } from "./stream_reader";
import { followWatch } from "./watch_stream";
import {
  DisconnectMcpServerResponseSchema,
  McpLinkageStatus,
  McpRefreshAction,
  StartMcpServerLinkageRequestSchema,
  StartMcpServerLinkageResponseSchema,
  WatchMcpServersResponseSchema,
  type McpServerLinkage,
} from "../../action_service/operator_action_service_pb";

const ACTION_FACADE = "/upstream/actions";

export type McpLinkageView = {
  server_id: string;
  server_url: string;
  status: "unlinked" | "linked" | "degraded" | "expired";
  scopes: string[];
  refresh_failure: {
    action: "reconnect" | "retrying";
    error: string;
    attempts: number;
    retry_at: string | null;
  } | null;
};

export type McpLinkageWatchFrame =
  | { kind: "snapshot"; linkages: McpLinkageView[] }
  | { kind: "snapshot_complete" }
  | { kind: "upsert"; linkage: McpLinkageView }
  | { kind: "reconnect_required" };

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function jsonValue(value: unknown): ProtoJsonValue {
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (Array.isArray(value)) return value.map(jsonValue);
  if (typeof value === "object") {
    const result: { [key: string]: ProtoJsonValue } = {};
    for (const [key, child] of Object.entries(value)) result[key] = jsonValue(child);
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
  return jsonValue(await response.json());
}

function required<T>(value: T | undefined, field: string): T {
  if (value === undefined) throw new Error(`The Action Service omitted ${field}.`);
  return value;
}

function status(value: McpLinkageStatus): McpLinkageView["status"] {
  switch (value) {
    case McpLinkageStatus.UNLINKED:
      return "unlinked";
    case McpLinkageStatus.LINKED:
      return "linked";
    case McpLinkageStatus.DEGRADED:
      return "degraded";
    case McpLinkageStatus.EXPIRED:
      return "expired";
    default:
      throw new Error(`The Action Service returned an unknown MCP linkage status (${value}).`);
  }
}

function refreshAction(value: McpRefreshAction): NonNullable<McpLinkageView["refresh_failure"]>["action"] {
  switch (value) {
    case McpRefreshAction.RECONNECT:
      return "reconnect";
    case McpRefreshAction.RETRYING:
      return "retrying";
    default:
      throw new Error(`The Action Service returned an unknown refresh action (${value}).`);
  }
}

function timestamp(value: Timestamp | undefined): string | null {
  if (value === undefined) return null;
  const json = toJson(TimestampSchema, value);
  if (typeof json !== "string") throw new Error("The Action Service returned an invalid timestamp.");
  return json;
}

function linkageView(value: McpServerLinkage): McpLinkageView {
  const failure = value.refreshFailure;
  return {
    server_id: value.serverId,
    server_url: value.serverUrl,
    status: status(value.status),
    scopes: value.scopes,
    refresh_failure:
      failure === undefined
        ? null
        : {
            action: refreshAction(failure.action),
            error: failure.error,
            attempts: failure.attempts,
            retry_at: timestamp(failure.retryAt),
          },
  };
}

export async function startMcpLinkage(
  serverId: string,
  scopes: string[],
  fetcher: Fetcher = fetchWithLogin
): Promise<string> {
  const request = create(StartMcpServerLinkageRequestSchema, { scopes });
  const response = fromJson(
    StartMcpServerLinkageResponseSchema,
    await readJson(
      `${ACTION_FACADE}/v1/operator/mcp-servers/${encodeURIComponent(serverId)}/linkage/start`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(toJson(StartMcpServerLinkageRequestSchema, request)),
      },
      fetcher
    )
  );
  const url = required(response.authorizationUrl, "linkage authorization URL");
  if (url === "") throw new Error("The Action Service returned an empty linkage authorization URL.");
  return url;
}

export async function disconnectMcpLinkage(
  serverId: string,
  fetcher: Fetcher = fetchWithLogin
): Promise<McpLinkageView> {
  const response = fromJson(
    DisconnectMcpServerResponseSchema,
    await readJson(
      `${ACTION_FACADE}/v1/operator/mcp-servers/${encodeURIComponent(serverId)}/linkage/disconnect`,
      { method: "POST" },
      fetcher
    )
  );
  return linkageView(required(response.linkage, "disconnected MCP linkage"));
}

export function followMcpLinkages(handlers: {
  onOpen: () => void;
  onFrame: (frame: McpLinkageWatchFrame) => void;
  onConnection: (connection: StreamConnection) => void;
}): () => void {
  return followWatch(`${ACTION_FACADE}/v1/operator/mcp-servers:watch`, {
    ...handlers,
    onFrame: (result) => {
      const response = fromJson(WatchMcpServersResponseSchema, jsonValue(result));
      switch (response.frame.case) {
        case "snapshot":
          handlers.onFrame({ kind: "snapshot", linkages: response.frame.value.linkages.map(linkageView) });
          break;
        case "snapshotComplete":
          handlers.onFrame({ kind: "snapshot_complete" });
          break;
        case "upsert":
          handlers.onFrame({ kind: "upsert", linkage: linkageView(response.frame.value) });
          break;
        case "reconnectRequired":
          handlers.onFrame({ kind: "reconnect_required" });
          break;
        case undefined:
          throw new Error("The Action Service returned an empty MCP linkage watch frame.");
      }
    },
  });
}
