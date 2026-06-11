import { get, writable } from "svelte/store";

import { currentAgentId, setAgentId } from "../../shared/router";
import { globalMcpClient, mcpManager } from "../mcp/manager";

import type { AgentRow, AgentStatus } from "../../shared/types";

export const agents = writable<AgentRow[]>([]);
export { currentAgentId, setAgentId };
export const agentStatus = writable<AgentStatus | null>(null);
export const agentStatusError = writable<string | null>(null);

// ----- MCP-based API functions -----

export interface PresetInfo {
  name: string;
  description?: string | null;
}

/**
 * List available agent presets via MCP.
 */
export async function listPresets(): Promise<{ presets: PresetInfo[] }> {
  const client = globalMcpClient();
  if (!client) {
    throw new Error("MCP client not connected");
  }
  const result = await client.readResource<PresetInfo[]>("agents://presets");
  return { presets: result || [] };
}

/**
 * Create a new agent from preset via MCP.
 */
export async function createAgentFromPreset(preset: string, _system?: string): Promise<{ id: string }> {
  const client = globalMcpClient();
  if (!client) {
    throw new Error("MCP client not connected");
  }
  // Note: system override not supported by current MCP tool
  const result = await client.callTool<{ id: string; status: string; preset: string }>("create_agent", { preset });
  return { id: result.id };
}

/**
 * Delete an agent via MCP.
 */
export async function deleteAgent(agentId: string): Promise<{ ok: boolean; error?: string }> {
  const client = globalMcpClient();
  if (!client) {
    throw new Error("MCP client not connected");
  }
  try {
    await client.callTool("delete_agent", { agent_id: agentId });
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/**
 * Get proposal content via MCP resource.
 */
export async function getProposal(proposalId: string): Promise<{ id: string; content: string }> {
  const agentId = get(currentAgentId);
  if (!agentId) {
    throw new Error("No agent selected");
  }
  const client = mcpManager.getClient(agentId);
  if (!client) {
    throw new Error("MCP client not connected to agent");
  }
  // The resource URI is resource://approval-policy/proposals/{id} which returns the content string
  const content = await client.readResource<string>(`resource://approval-policy/proposals/${proposalId}`);
  return { id: proposalId, content: content || "" };
}

let _agentsUnsubscribe: (() => void) | null = null;

/**
 * Start MCP-based agents subscription.
 * Uses the global MCP client to subscribe to agents://list resource.
 */
export async function startAgentsSubscription() {
  stopAgentsSubscription();
  const client = globalMcpClient();
  if (!client) {
    console.error("Global MCP client not available");
    return;
  }
  try {
    _agentsUnsubscribe = await client.subscribeResource<{ agents: AgentRow[] }>("agents://list", (data) => {
      agents.set(data.agents || []);
    });
  } catch (e) {
    console.error("MCP agents subscription failed", e);
  }
}

export function stopAgentsSubscription() {
  if (_agentsUnsubscribe) {
    _agentsUnsubscribe();
    _agentsUnsubscribe = null;
  }
}
