import { writable, derived, type Writable, type Readable } from "svelte/store";
import { get } from "svelte/store";

import { currentAgentId, agentStatus } from "../agents/stores";
import { mcpManager } from "../mcp/manager";

import type { SnapshotPayload, SamplingSnapshot, UiState, ServerEntry, ApprovalPolicyInfo } from "../../shared/types";
import type { AgentMcpClient } from "../mcp/client";

export type Pending = { call_id: string; tool_key: string; args_json?: string | null };

export const agentPhase: Writable<string> = writable("idle");
// Agent-scoped UI state
export const uiStates: Writable<Map<string, UiState>> = writable(new Map());
export const uiState: Readable<UiState | null> = derived([uiStates, currentAgentId], ([$uiStates, $current]) =>
  $current ? ($uiStates.get($current) ?? null) : null
);
export const lastError: Writable<string | null> = writable(null);
export const pendingApprovals: Writable<Map<string, Pending>> = writable(new Map());
export const approvalPolicy: Writable<ApprovalPolicyInfo | null> = writable(null);
export const mcpServerEntries: Writable<ServerEntry[]> = writable([]);

let currentClient: AgentMcpClient | null = null;
let approvalsUnsubscribe: (() => void) | null = null;

export function clearError() {
  lastError.set(null);
}

/** Log error to console and set lastError store with consistent formatting. */
function handleError(label: string, e: unknown): void {
  const msg = e instanceof Error ? e.message : String(e);
  console.warn(`${label} failed`, e);
  lastError.set(`${label} failed: ${msg}`);
}

export async function connectAgentMcp(agentId: string) {
  // Disconnect any existing
  await disconnectAgentMcp();

  try {
    // Connect to agent's MCP compositor
    currentClient = await mcpManager.connectAgent(agentId);

    // Mark agent as live
    agentStatus.set({ id: agentId, live: true });

    // Subscribe to pending approvals resource
    approvalsUnsubscribe = await currentClient.subscribeResource<{ pending: Pending[] }>(
      "approvals://pending",
      (data) => {
        const map = new Map(data.pending.map((p) => [p.call_id, p]));
        pendingApprovals.set(map);
      }
    );

    // Initial snapshot fetch
    await refreshSnapshot();
  } catch (e) {
    handleError("MCP connection", e);
    agentStatus.set({ id: agentId, live: false });
  }
}

export async function disconnectAgentMcp() {
  if (approvalsUnsubscribe) {
    approvalsUnsubscribe();
    approvalsUnsubscribe = null;
  }

  if (currentClient) {
    const agentId = get(currentAgentId);
    if (agentId) {
      await mcpManager.disconnectAgent(agentId);
    }
    currentClient = null;
  }
}

// --- Tool Actions (MCP-based) ---

export async function sendPrompt(text: string) {
  // Optimistically reflect starting state
  agentPhase.set("starting");
  if (!currentClient) {
    lastError.set("Not connected to agent");
    return;
  }
  try {
    // Use MCP tool: agent_control_send_prompt
    await currentClient.callTool("agent_control_send_prompt", { prompt: text });
  } catch (e) {
    handleError("Send prompt", e);
  }
}

export async function approve(call_id: string) {
  if (!currentClient) return;
  try {
    await currentClient.callTool("approvals_approve_call", { call_id });
  } catch (e) {
    handleError("Approve", e);
  }
  // Pending approvals will update via resource subscription
}

export async function denyContinue(call_id: string) {
  if (!currentClient) return;
  try {
    await currentClient.callTool("approvals_deny_continue", { call_id });
  } catch (e) {
    handleError("Deny continue", e);
  }
}

export async function deny(call_id: string) {
  if (!currentClient) return;
  try {
    await currentClient.callTool("approvals_deny_abort", { call_id });
  } catch (e) {
    handleError("Deny abort", e);
  }
}

export async function setPolicy(content: string, proposal_id?: string) {
  if (!currentClient) return;
  try {
    await currentClient.callTool("approval_policy.admin_set_policy", {
      content,
      proposal_id: proposal_id ?? null,
    });
  } catch (e) {
    handleError("Set policy", e);
  }
  await refreshSnapshot();
}

export async function approveProposal(proposal_id: string) {
  if (!currentClient) return;
  try {
    // Approve proposal via unified decide_proposal MCP tool
    await currentClient.callTool("approval_policy.admin_decide_proposal", {
      proposal_id,
      decision: "approve",
    });
  } catch (e) {
    handleError("Approve proposal", e);
  }
  await refreshSnapshot();
}

export async function rejectProposal(proposal_id: string) {
  if (!currentClient) return;
  try {
    // Reject proposal via unified decide_proposal MCP tool
    await currentClient.callTool("approval_policy.admin_decide_proposal", {
      proposal_id,
      decision: "reject",
    });
  } catch (e) {
    handleError("Reject proposal", e);
  }
  await refreshSnapshot();
}

export async function withdrawProposal(proposal_id: string) {
  if (!currentClient) return;
  try {
    await currentClient.callTool("approval_policy.proposer_withdraw_proposal", {
      id: proposal_id,
    });
  } catch (e) {
    handleError("Withdraw proposal", e);
  }
  await refreshSnapshot();
}

export async function refreshSnapshot() {
  if (!currentClient) return;
  try {
    // Read snapshot via MCP resource
    const snap = await currentClient.readResource<SnapshotPayload>("snapshot://current");
    handleSnapshot(snap);
  } catch (e) {
    console.warn("refreshSnapshot failed", e);
  }
}

export async function abortAgent() {
  if (!currentClient) return;
  try {
    // Use MCP tool: agent_control_abort
    await currentClient.callTool("agent_control_abort", {});
  } catch (e) {
    handleError("Abort agent", e);
  }
  await refreshSnapshot();
}

export async function reconfigureMcp(attach?: Record<string, any>, detach?: string[]) {
  if (!currentClient) return;
  try {
    // Use MCP tools for attach/detach via compositor_admin
    if (attach && Object.keys(attach).length) {
      for (const [name, spec] of Object.entries(attach)) {
        await currentClient.callTool("compositor_admin_attach_server", { name, spec });
      }
    }
    if (detach && detach.length) {
      for (const name of detach) {
        await currentClient.callTool("compositor_admin_detach_server", { name });
      }
    }
  } catch (e) {
    handleError("Reconfigure MCP", e);
  }
  await refreshSnapshot();
}

/**
 * Attach a new MCP server to the current agent.
 */
export async function attachMcpServer(name: string, spec: any): Promise<void> {
  if (!currentClient) {
    throw new Error("Not connected to agent");
  }
  await currentClient.callTool("compositor_admin_attach_server", { name, spec });
}

/**
 * Detach an MCP server from the current agent.
 */
export async function detachMcpServer(name: string): Promise<void> {
  if (!currentClient) {
    throw new Error("Not connected to agent");
  }
  await currentClient.callTool("compositor_admin_detach_server", { name });
}

function handleSnapshot(p: SnapshotPayload) {
  const sampling: SamplingSnapshot | undefined = p.sampling as any;
  if (sampling) {
    mcpServerEntries.set(sampling.servers || []);
  }
  // Run state removed - status now tracked via AgentStatus resource
  agentPhase.set("idle");

  // Pending approvals are now updated via MCP resource subscription only
  // (no longer in snapshot)

  if (p.approval_policy) approvalPolicy.set(p.approval_policy);
}
