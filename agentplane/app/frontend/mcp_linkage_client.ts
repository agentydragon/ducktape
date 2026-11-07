import { create, fromJson, fromJsonString, toJson } from "@bufbuild/protobuf";
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

export type McpLinkageWatchFrame =
  | { kind: "snapshot"; linkages: McpServerLinkage[] }
  | { kind: "snapshot_complete" }
  | { kind: "upsert"; linkage: McpServerLinkage }
  | { kind: "reconnect_required" };

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

async function readText(url: string, init: RequestInit = {}, fetcher: Fetcher = fetchWithLogin): Promise<string> {
  const response = await fetcher(url, init);
  if (!response.ok) throw new HttpError(response, await response.text());
  return response.text();
}

function required<T>(value: T | undefined, field: string): T {
  if (value === undefined) throw new Error(`The Action Service omitted ${field}.`);
  return value;
}

function validateLinkageStatus(value: McpLinkageStatus): void {
  switch (value) {
    case McpLinkageStatus.UNLINKED:
    case McpLinkageStatus.LINKED:
    case McpLinkageStatus.DEGRADED:
    case McpLinkageStatus.EXPIRED:
      return;
    default:
      throw new Error(`The Action Service returned an unknown MCP linkage status (${value}).`);
  }
}

function validateRefreshAction(value: McpRefreshAction): void {
  switch (value) {
    case McpRefreshAction.RECONNECT:
    case McpRefreshAction.RETRYING:
      return;
    default:
      throw new Error(`The Action Service returned an unknown refresh action (${value}).`);
  }
}

function validateLinkage(value: McpServerLinkage): McpServerLinkage {
  validateLinkageStatus(value.status);
  if (value.refreshFailure !== undefined) validateRefreshAction(value.refreshFailure.action);
  return value;
}

export async function startMcpLinkage(
  serverId: string,
  scopes: string[],
  fetcher: Fetcher = fetchWithLogin
): Promise<string> {
  const request = create(StartMcpServerLinkageRequestSchema, { scopes });
  const response = fromJsonString(
    StartMcpServerLinkageResponseSchema,
    await readText(
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
): Promise<McpServerLinkage> {
  const response = fromJsonString(
    DisconnectMcpServerResponseSchema,
    await readText(
      `${ACTION_FACADE}/v1/operator/mcp-servers/${encodeURIComponent(serverId)}/linkage/disconnect`,
      { method: "POST" },
      fetcher
    )
  );
  return validateLinkage(required(response.linkage, "disconnected MCP linkage"));
}

export function followMcpLinkages(handlers: {
  onOpen: () => void;
  onFrame: (frame: McpLinkageWatchFrame) => void;
  onConnection: (connection: StreamConnection) => void;
}): () => void {
  return followWatch(`${ACTION_FACADE}/v1/operator/mcp-servers:watch`, {
    ...handlers,
    onFrame: (result) => {
      const response = fromJson(WatchMcpServersResponseSchema, result);
      switch (response.frame.case) {
        case "snapshot":
          handlers.onFrame({ kind: "snapshot", linkages: response.frame.value.linkages.map(validateLinkage) });
          break;
        case "snapshotComplete":
          handlers.onFrame({ kind: "snapshot_complete" });
          break;
        case "upsert":
          handlers.onFrame({ kind: "upsert", linkage: validateLinkage(response.frame.value) });
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
