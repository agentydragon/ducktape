<script lang="ts">
  import "../styles/shared.css";
  import ApprovalsPanel from "./ApprovalsPanel.svelte";
  import ServersPanel from "./ServersPanel.svelte";
  import SettingsPanel from "./SettingsPanel.svelte";
  import {
    agentStatus as agentStatusStore,
    agentStatusError as agentStatusErrorStore,
  } from "../features/agents/stores";
  import {
    agentPhase as agentPhaseStore,
    pendingApprovals,
    approvalPolicy as approvalPolicyStore,
    mcpServerEntries as mcpServerEntriesStore,
    lastError as lastErrorStore,
    clearError as clearWsError,
    approve as wsApprove,
    denyContinue as wsDenyContinue,
    deny as wsDeny,
    setPolicy as wsSetPolicy,
    approveProposal as wsApproveProposal,
    withdrawProposal as wsWithdrawProposal,
  } from "../features/chat/stores";
  import { mcpManager } from "../features/mcp/manager";

  // Derive connection status from MCP manager
  const mcpConnected = mcpManager.connectionStatus;

  // Local UI state
  let activeTab: "approvals" | "servers" | "settings" = "approvals";
  let showPolicyEditor = false;
  let editingPolicy = "";

  export let deleteCurrentAgent: () => void;

  function startEditingPolicy() {
    editingPolicy = $approvalPolicyStore?.content || "";
    showPolicyEditor = true;
  }
  function cancelEditingPolicy() {
    showPolicyEditor = false;
    editingPolicy = "";
  }
</script>

<div class="sidebar-header">
  <div
    class="ws"
    title={$mcpConnected.connected
      ? "MCP connected (browser ↔ server). Controls live updates; not agent liveness."
      : "MCP disconnected (browser ↔ server). Live updates paused; not agent liveness."}
  >
    <span class="dot {$mcpConnected.connected ? 'on' : 'off'}"></span>
    <span>{$mcpConnected.connected ? "MCP connected" : "MCP disconnected"}</span>
  </div>
  <div class="status">Phase: {$agentPhaseStore}</div>
  {#if $agentStatusStore}
    <div class="row" style="gap: 0.5rem; font-size: 0.85rem; margin-top: 0.25rem;">
      <span title="Lifecycle"
        >Lifecycle: {$agentStatusStore.lifecycle ?? ($agentStatusStore.live ? "ready" : "persisted_only")}</span
      >

      {#if $agentStatusStore.policy}
        <span title="Policy">
          Policy: {typeof $agentStatusStore.policy.version === "number"
            ? `v${$agentStatusStore.policy.version}`
            : "unavailable"}
        </span>
      {/if}
      {#if $agentStatusStore.mcp}
        <span title="MCP servers"
          >MCP: {Object.values($agentStatusStore.mcp.entries || {}).filter((e: any) => e?.state !== "running").length} failed</span
        >
      {/if}
      {#if $agentStatusStore.container}
        <span
          title={$agentStatusStore.container.id ? `Container ${$agentStatusStore.container.id}` : "Runtime container"}
        >
          Runtime: {$agentStatusStore.container.id ? "active" : "starting"}
        </span>
      {/if}
    </div>
    <!-- Live mounts summary (derived from snapshots; updates on MCP notifications) -->
    {#if Array.isArray($mcpServerEntriesStore) && $mcpServerEntriesStore.length}
      <div class="mounts">
        {#each $mcpServerEntriesStore as m (m.name)}
          {@const errorMsg = m.state === "failed" ? m.error : null}
          <div class="mount-item" title={`server ${m.name}: ${m.state}${errorMsg ? " — " + errorMsg : ""}`}>
            <span class="dot {m.state === 'running' ? 'on' : 'off'}"></span>
            <span class="name">{m.name}</span>
          </div>
        {/each}
      </div>
    {/if}
  {/if}
  {#if $agentStatusErrorStore}
    <div class="status-error" title={$agentStatusErrorStore}>Agent status unavailable</div>
  {/if}
  <!-- Left sidebar toggle moved out of right sidebar -->
  <!-- Agent selection moved to left sidebar (AgentsSidebar). -->
</div>

<div class="tabs">
  <button class="tab {activeTab === 'approvals' ? 'active' : ''}" on:click={() => (activeTab = "approvals")}>
    Approvals{#if Array.from($pendingApprovals.values()).length > 0}
      <span class="badge">{Array.from($pendingApprovals.values()).length}</span>{/if}
  </button>
  <button class="tab {activeTab === 'servers' ? 'active' : ''}" on:click={() => (activeTab = "servers")}>
    MCP ({$mcpServerEntriesStore.length})
  </button>
  <button class="tab {activeTab === 'settings' ? 'active' : ''}" on:click={() => (activeTab = "settings")}>
    Settings
  </button>
</div>

<div class="tab-content">
  {#if activeTab === "approvals"}
    <ApprovalsPanel
      approvalPolicy={$approvalPolicyStore}
      {showPolicyEditor}
      {editingPolicy}
      {startEditingPolicy}
      {cancelEditingPolicy}
      setPolicy={wsSetPolicy}
      approveProposal={(id) => wsApproveProposal(id)}
      rejectProposal={(id) => wsWithdrawProposal(id)}
      approve={wsApprove}
      denyContinue={wsDenyContinue}
      deny={wsDeny}
      pending={Array.from($pendingApprovals.values())}
    />
  {:else if activeTab === "servers"}
    <ServersPanel servers={$mcpServerEntriesStore} />
  {:else}
    <SettingsPanel lastError={$lastErrorStore} clearError={() => clearWsError()} {deleteCurrentAgent} />
  {/if}
</div>

<style>
  :global(#right-sidebar) {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .sidebar-header {
    padding: 0.5rem;
    border-bottom: 1px solid var(--border);
  }
  .ws {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .status {
    color: var(--text);
  }
  .status-error {
    color: #b00020;
    font-size: 0.75rem;
    margin-top: 0.25rem;
  }
  /* Removed unused legacy agent-badge/agent-dot styles */
  .badge {
    background: var(--surface-3);
    border-radius: 0.75rem;
    padding: 0 0.4rem;
    font-size: 0.7rem;
  }
  .mounts {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.75rem;
    margin-top: 0.35rem;
  }
  .mount-item {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.1rem 0.35rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 0.75rem;
  }
  .mount-item .name {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  }
  .tabs {
    display: flex;
    gap: 0.25rem;
    padding: 0.5rem;
    border-bottom: 1px solid var(--border);
  }
  .tab {
    padding: 0.25rem 0.5rem;
    border: 1px solid var(--border);
    border-bottom: none;
    background: var(--surface-2);
    color: var(--text);
    cursor: pointer;
  }
  .tab.active {
    background: var(--surface);
    font-weight: 600;
  }
  .tab-content {
    padding: 0.5rem;
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
  }
  /* Removed unused .small */
</style>
