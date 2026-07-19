import { z } from "zod";

import { callOperatorMcpTool } from "./mcp_client.ts";

const zMcpOAuthConnection = z.discriminatedUnion("status", [
  z.object({ server_id: z.string(), username: z.string(), status: z.literal("unconnected") }),
  z.object({
    server_id: z.string(),
    username: z.string(),
    status: z.literal("connected"),
    connected_at: z.string(),
    token_expires_at: z.string().nullable(),
    scope: z.string().nullable(),
  }),
]);

const zProviderConnection = z.discriminatedUnion("status", [
  z.object({
    connection: z.string(),
    display_name: z.string(),
    provider: z.string(),
    status: z.literal("unconnected"),
  }),
  z.object({
    connection: z.string(),
    display_name: z.string(),
    provider: z.string(),
    status: z.literal("connected"),
    connected_at: z.string(),
    token_expires_at: z.string().nullable(),
    scope: z.string().nullable(),
  }),
]);

const zMcpServerConnection = z.object({
  server_id: z.string(),
  auth_kind: z.string(),
  connection: z.union([zMcpOAuthConnection, zProviderConnection]).nullable(),
});

const zTool = z.object({
  name: z.string(),
  description: z.string().nullable(),
  input_schema: z.record(z.string(), z.unknown()).nullable(),
  annotations: z.record(z.string(), z.unknown()).nullable(),
});

const zServerBase = z.object({ server_id: z.string(), title: z.string(), tools: z.array(zTool) });
const zServerReflection = z.discriminatedUnion("status", [
  zServerBase.extend({ status: z.literal("alive") }),
  zServerBase.extend({
    status: z.literal("degraded"),
    failure_stage: z.enum(["credential_resolution", "tool_discovery"]),
    degraded_reason: z.string(),
  }),
]);

const zMcpServerList = z.object({ servers: z.array(zMcpServerConnection) });
const zMcpServerProbe = z.object({ connection: zMcpServerConnection, server: zServerReflection });
const zDaemonStatus = z.object({
  daemon_id: z.string(),
  display_name: z.string(),
  status: z.enum(["connected", "busy", "stale", "offline"]),
  last_heartbeat_at: z.string().nullable(),
  version: z.string().nullable(),
  backends: z.array(z.string()),
  active_execution_id: z.string().nullable(),
});
const zDaemonStatuses = z.object({ daemons: z.array(zDaemonStatus) });

export type McpServerConnection = z.infer<typeof zMcpServerConnection>;
export type McpServerProbe = z.infer<typeof zMcpServerProbe>;
export type DaemonStatus = z.infer<typeof zDaemonStatus>;

export async function listMcpServers(): Promise<McpServerConnection[]> {
  return zMcpServerList.parse(await callOperatorMcpTool("list_mcp_servers", {})).servers;
}

export async function getMcpServerStatus(serverId: string): Promise<McpServerProbe> {
  return zMcpServerProbe.parse(
    await callOperatorMcpTool("get_mcp_server_status", { server_id: serverId, include_tool_schemas: false })
  );
}

export async function listNodeDaemons(): Promise<DaemonStatus[]> {
  return zDaemonStatuses.parse(await callOperatorMcpTool("list_node_daemons", {})).daemons;
}
