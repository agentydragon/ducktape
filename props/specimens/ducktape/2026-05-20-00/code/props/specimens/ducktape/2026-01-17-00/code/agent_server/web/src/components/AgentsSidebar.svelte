<script lang="ts">
  import "../styles/shared.css";
  import { onMount } from "svelte";

  import ModalBackdrop from "./ModalBackdrop.svelte";
  import SidebarToggle from "./SidebarToggle.svelte";
  import {
    agents,
    currentAgentId,
    setAgentId,
    deleteAgent,
    listPresets,
    createAgentFromPreset,
  } from "../features/agents/stores";
  import { LEFT_MIN, LEFT_MAX } from "../shared/layout";
  import { prefs } from "../shared/prefs";

  import type { AgentRow } from "../shared/types";

  export let onStartResize: (_e: MouseEvent) => void;

  $: agentList = $agents as AgentRow[];
  $: selected = $currentAgentId;
  let listEl: HTMLDivElement | null = null;
  let presets: Array<{ name: string; description?: string | null }> = [];
  let selectedPreset: string | null = null;
  // Modal state for preset selection
  let showPresetModal = false;
  let modalPreset: string | null = null;
  let modalSystem: string = "";
  // Restore scroll position

  async function refreshPresets() {
    try {
      const r = await listPresets();
      const list = r.presets || [];
      presets = list;
      if (showPresetModal) {
        if (!modalPreset || !list.find((p) => p.name === modalPreset)) {
          modalPreset = list[0]?.name || null;
        }
      }
    } catch {
      // Ignore refresh failure - UI continues with empty preset list
    }
  }
  onMount(() => {
    try {
      const saved = localStorage.getItem("agentsSidebarScrollTop");
      if (saved && listEl) listEl.scrollTop = parseInt(saved, 10) || 0;
    } catch {
      // Ignore localStorage errors - scroll position is not critical
    }
    // Initial load
    refreshPresets();
    // Auto-refresh on focus and tab visibility
    const onFocus = () => {
      void refreshPresets();
    };
    const onVis = () => {
      if (document.visibilityState === "visible") void refreshPresets();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVis);
    };
  });

  function open(id: string) {
    setAgentId(id);
  }

  // Inline delete confirm state
  let confirmingId: string | null = null;

  async function openPresetDialog() {
    // Always refresh presets from backend so filesystem edits are reflected immediately
    try {
      const r = await listPresets();
      presets = r.presets || [];
    } catch {
      // Keep prior list on fetch failure
    }
    if (!presets || presets.length === 0) {
      // No presets available; create default immediately
      try {
        const body = await createAgentFromPreset("default");
        const id = body?.id as string;
        if (id) setAgentId(id);
      } catch (e) {
        console.warn("create default preset failed", e);
      }
      return;
    }
    // Initialize modal selection after refresh
    modalPreset = selectedPreset || presets[0]?.name || null;
    modalSystem = "";
    showPresetModal = true;
  }

  async function confirmCreateFromModal() {
    if (!modalPreset) return;
    try {
      const body = await createAgentFromPreset(modalPreset, modalSystem || undefined);
      const id = body?.id as string;
      if (id) setAgentId(id);
    } catch (e) {
      console.warn("create agent from modal failed", e);
    } finally {
      showPresetModal = false;
    }
  }

  async function doDelete(id: string) {
    try {
      const body = await deleteAgent(id);
      if (!body?.ok) throw new Error(body?.error || "delete failed");
      // Optimistically update list and selection
      agents.update((list) => (list || []).filter((a) => a.id !== id));
      if ($currentAgentId === id) setAgentId(null);
      confirmingId = null;
    } catch (e) {
      console.warn("delete failed", e);
    }
  }

  function lastUpdatedTitle(a: AgentRow): string {
    const lu = a.last_updated ? `\nlast updated: ${a.last_updated}` : "";
    const s = a.working
      ? "working (active run in progress)"
      : a.live
        ? "on (live container running)"
        : "off (no live container)";
    return `Agent ${a.id}\n${s}${lu}`;
  }

  function lifecycleLabel(a: AgentRow): string {
    const lc = a.lifecycle;
    if (lc === "ready") return "ready";
    if (lc === "starting") return "starting";
    if (lc === "persisted_only") return "persisted";
    // Fallback when lifecycle not provided yet
    return a.live ? "ready" : "persisted";
  }
</script>

<aside class="leftbar" id="agents-sidebar">
  <div class="leftbar-header">
    <div class="row">
      <strong>Agents</strong>
      <button class="small add" title="Create new agent" aria-label="Create new agent" on:click={openPresetDialog}
        >+</button
      >
      <SidebarToggle
        title="Hide agents sidebar"
        label="Hide agents sidebar"
        glyph="«"
        action={() => prefs.update((p) => ({ ...p, showAgentsSidebar: false }))}
      />
    </div>
    <!-- hint removed -->
  </div>
  <div
    class="agents-list"
    bind:this={listEl}
    on:scroll={() => {
      try {
        localStorage.setItem("agentsSidebarScrollTop", String(listEl?.scrollTop || 0));
      } catch {
        // Ignore localStorage errors - scroll position persistence is not critical
      }
    }}
  >
    {#each agentList as a (a.id)}
      <div
        class="agent-row {a.id === selected ? 'current' : ''}"
        title={lastUpdatedTitle(a)}
        aria-current={a.id === selected ? "true" : undefined}
        role="button"
        tabindex="0"
        on:click={() => open(a.id)}
        on:keydown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            open(a.id);
          }
        }}
      >
        <button
          type="button"
          class="agent-open"
          on:click={(e) => {
            e.stopPropagation();
            open(a.id);
          }}
        >
          <span class="dot {a.working ? 'working' : a.live ? 'on' : 'off'}"></span>
          <span class="agent-id">{a.id.slice(0, 8)}</span>
          <span
            class="badge lifecycle lc-{a.lifecycle ?? (a.live ? 'ready' : 'persisted_only')}"
            title={`Lifecycle: ${lifecycleLabel(a)}`}>{lifecycleLabel(a)}</span
          >
          {#if a.metadata?.preset}
            <span class="preset" title={`Preset: ${a.metadata.preset}`}>{a.metadata.preset}</span>
          {/if}
        </button>
        <div class="row-actions">
          {#if confirmingId === a.id}
            <button class="danger small" on:click|stopPropagation={() => doDelete(a.id)}>Confirm</button>
            <button class="secondary small" on:click|stopPropagation={() => (confirmingId = null)}>Cancel</button>
          {:else}
            <button
              class="danger small icon"
              aria-label="Delete agent"
              title="Delete agent"
              on:click|stopPropagation={() => (confirmingId = a.id)}>×</button
            >
          {/if}
        </div>
      </div>
    {/each}
  </div>
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <!-- svelte-ignore a11y-no-noninteractive-tabindex -->
  <div
    class="left-resize"
    role="separator"
    aria-orientation="vertical"
    aria-controls="agents-sidebar"
    aria-valuemin={LEFT_MIN}
    aria-valuemax={LEFT_MAX}
    aria-valuenow={$prefs.leftSidebarWidth}
    tabindex="0"
    on:mousedown={onStartResize}
    on:keydown={(e) => {
      const step = e.shiftKey ? 32 : 8;
      if (e.key === "ArrowLeft") {
        prefs.update((p) => ({
          ...p,
          leftSidebarWidth: Math.max(LEFT_MIN, p.leftSidebarWidth - step),
        }));
        e.preventDefault();
      } else if (e.key === "ArrowRight") {
        prefs.update((p) => ({
          ...p,
          leftSidebarWidth: Math.min(LEFT_MAX, p.leftSidebarWidth + step),
        }));
        e.preventDefault();
      }
    }}
    title="Drag to resize"
    aria-label="Resize agents sidebar"
  ></div>
</aside>

{#if showPresetModal}
  <ModalBackdrop label="Close create agent dialog" onClose={() => (showPresetModal = false)}>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Create agent from preset">
      <header>Create Agent</header>
      <div class="body">
        {#if presets.length > 0}
          <div class="row">
            <label for="modal-preset" style="min-width: 80px;">Preset</label>
            <select id="modal-preset" bind:value={modalPreset} style="flex: 1;">
              {#each presets as p (p.name)}
                <option value={p.name}>{p.name}{p.description ? ` — ${p.description}` : ""}</option>
              {/each}
            </select>
          </div>
        {:else}
          <div>No presets found. This will use the built-in default.</div>
        {/if}
        <div class="row">
          <label for="modal-system" style="min-width: 80px;">System</label>
          <input
            id="modal-system"
            type="text"
            placeholder="(optional) override system message"
            bind:value={modalSystem}
            style="flex: 1;"
          />
        </div>
      </div>
      <footer>
        <button class="secondary" on:click={() => (showPresetModal = false)}>Cancel</button>
        <button class="primary" on:click={confirmCreateFromModal} disabled={!modalPreset && presets.length > 0}
          >Create</button
        >
      </footer>
    </div>
  </ModalBackdrop>
{/if}

<style>
  .leftbar {
    display: flex;
    flex-direction: column;
    position: relative;
    width: 100%;
    min-width: 0;
  }
  .leftbar-header {
    padding: 0.5rem;
    border-bottom: 1px solid var(--border);
  }
  .agents-list {
    overflow-y: auto;
    padding: 0.25rem 0.5rem;
  }
  .agent-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.25rem 0.25rem;
    border-radius: 4px;
    width: 100%;
    cursor: pointer;
  }
  .agent-row:hover {
    background: var(--surface-2);
  }
  .agent-row.current {
    background: rgba(25, 118, 210, 0.1);
  }
  .agent-open {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    background: none;
    border: 1px solid transparent;
    cursor: pointer;
    padding: 0.2rem 0.25rem;
  }
  .agent-id {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.85rem;
  }
  .badge.lifecycle.lc-ready {
    background: rgba(46, 204, 113, 0.15);
    color: #2e7d32;
    border-color: rgba(46, 204, 113, 0.4);
  }
  .badge.lifecycle.lc-starting {
    background: rgba(255, 193, 7, 0.15);
    color: #b26a00;
    border-color: rgba(255, 193, 7, 0.4);
  }
  .badge.lifecycle.lc-persisted_only {
    background: rgba(158, 158, 158, 0.15);
    color: #616161;
    border-color: rgba(158, 158, 158, 0.4);
  }
  .dot.working {
    background: #f39c12;
    animation: blink 1s infinite ease-in-out;
  }
  @keyframes blink {
    0%,
    100% {
      opacity: 0.5;
    }
    50% {
      opacity: 1;
    }
  }
  /* Keep the resize handle within the sidebar to avoid overlaying the chat area */
  .left-resize {
    position: absolute;
    top: 0;
    right: 0;
    width: 6px;
    height: 100%;
    cursor: col-resize;
    background: transparent;
    border: none;
    padding: 0;
  }
  .preset {
    flex: 1;
    min-width: 0;
  }
  .row :global(.toggle-btn) {
    margin-left: auto;
  }
  .row-actions {
    margin-left: auto;
    display: inline-flex;
    gap: 0.25rem;
  }
  .icon {
    width: 22px;
    height: 22px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
  }
  .add {
    margin-left: auto;
  }
</style>
