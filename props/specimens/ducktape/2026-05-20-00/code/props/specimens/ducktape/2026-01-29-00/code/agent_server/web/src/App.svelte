<script lang="ts">
  import { onMount, onDestroy } from "svelte";

  import AgentsSidebar from "./components/AgentsSidebar.svelte";
  import ChatPane from "./components/ChatPane.svelte";
  import RightSidebar from "./components/RightSidebar.svelte";
  import SidebarToggle from "./components/SidebarToggle.svelte";
  import { deleteAgent } from "./features/agents/stores";
  import { disconnectAgentMcp } from "./features/chat/stores";
  import { initAgentUiController } from "./features/controller";
  import { prefs } from "./shared/prefs";
  import { currentAgentId as currentAgentIdStore, setAgentId } from "./shared/router";

  let hasAgent = false;
  // Current agent id
  let agentId: string | null = null;

  onDestroy(() => {
    disconnectAgentMcp();
  });

  // Resize functionality
  let isLeftResizing = false;
  let isSplitResizing = false;
  const SPLIT_MIN_TOP = 120;
  const SPLIT_MIN_BOTTOM = 180;

  function startLeftResize() {
    isLeftResizing = true;
    document.addEventListener("mousemove", handleLeftResize);
    document.addEventListener("mouseup", stopLeftResize);
  }
  function handleLeftResize(e: MouseEvent) {
    if (!isLeftResizing) return;
    const newWidth = e.clientX;
    const w = Math.max(160, Math.min(500, newWidth));
    prefs.update((p) => ({ ...p, leftSidebarWidth: w }));
  }
  // Top/bottom split resize (inside left pane)
  function startSplitResize() {
    isSplitResizing = true;
    document.addEventListener("mousemove", handleSplitResize);
    document.addEventListener("mouseup", stopSplitResize);
  }
  function handleSplitResize(e: MouseEvent) {
    if (!isSplitResizing) return;
    // Compute relative to viewport; left pane takes full height
    const minTop = SPLIT_MIN_TOP;
    const minBottom = SPLIT_MIN_BOTTOM;
    const total = window.innerHeight;
    const y = e.clientY;
    const top = Math.max(minTop, Math.min(total - minBottom, y));
    prefs.update((p) => ({ ...p, leftTopHeight: top }));
  }
  function stopSplitResize() {
    isSplitResizing = false;
    document.removeEventListener("mousemove", handleSplitResize);
    document.removeEventListener("mouseup", stopSplitResize);
  }
  function stopLeftResize() {
    isLeftResizing = false;
    document.removeEventListener("mousemove", handleLeftResize);
    document.removeEventListener("mouseup", stopLeftResize);
  }

  // No-op placeholders removed; controller manages WS + polling
  async function deleteCurrentAgent() {
    if (!agentId) return;
    try {
      const body = await deleteAgent(agentId);
      if (!body?.ok) {
        throw new Error(body?.error || "delete failed");
      }
      // Clear current agent; center pane shows AgentsList
      setAgentId(null);
    } catch (e: any) {
      console.error(e);
    }
  }

  // React to agent id changes via store
  $: {
    const id = $currentAgentIdStore;
    agentId = id;
    hasAgent = !!id;
  }

  onMount(() => {
    const disposeCtrl = initAgentUiController();
    onDestroy(() => {
      disposeCtrl();
    });
  });
</script>

<main
  class="shell"
  style="grid-template-columns: {$prefs.showAgentsSidebar ? `${$prefs.leftSidebarWidth}px 1fr` : `1fr`}"
>
  {#if $prefs.showAgentsSidebar}
    <div class="left-stack" style="grid-column: 1; grid-row: 1; position: relative;">
      <div class="left-top" style={`height: ${$prefs.leftTopHeight}px;`}>
        <AgentsSidebar onStartResize={startLeftResize} />
      </div>
      <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
      <!-- svelte-ignore a11y-no-noninteractive-tabindex -->
      <div
        class="split-resize"
        role="separator"
        aria-orientation="horizontal"
        aria-valuemin={SPLIT_MIN_TOP}
        aria-valuemax={window.innerHeight - SPLIT_MIN_BOTTOM}
        aria-valuenow={$prefs.leftTopHeight}
        tabindex="0"
        on:mousedown={startSplitResize}
        on:keydown={(e) => {
          const step = e.shiftKey ? 40 : 10;
          if (e.key === "ArrowUp") {
            prefs.update((p) => ({
              ...p,
              leftTopHeight: Math.max(SPLIT_MIN_TOP, p.leftTopHeight - step),
            }));
            e.preventDefault();
          } else if (e.key === "ArrowDown") {
            prefs.update((p) => ({
              ...p,
              leftTopHeight: Math.min(window.innerHeight - SPLIT_MIN_BOTTOM, p.leftTopHeight + step),
            }));
            e.preventDefault();
          }
        }}
        title="Drag to resize"
      ></div>
      <div class="left-bottom">
        <div class="bottompane">
          <RightSidebar {deleteCurrentAgent} />
        </div>
      </div>
      <!-- Full-height vertical resize handle for the left column width -->
      <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
      <!-- svelte-ignore a11y-no-noninteractive-tabindex -->
      <div
        class="left-col-resize"
        role="separator"
        aria-orientation="vertical"
        tabindex="0"
        on:mousedown={startLeftResize}
        title="Drag to resize left column"
      ></div>
    </div>
  {/if}
  {#if !$prefs.showAgentsSidebar}
    <SidebarToggle
      title="Show agents sidebar"
      label="Show agents sidebar"
      glyph="»"
      extraClass="floating"
      action={() => prefs.update((p) => ({ ...p, showAgentsSidebar: true }))}
    />
  {/if}
  {#if hasAgent}
    <ChatPane
      renderMarkdown={$prefs.renderMarkdown}
      style={`grid-column: ${$prefs.showAgentsSidebar ? 2 : 1}; grid-row: 1; min-width: 0; min-height: 0;`}
    />
  {:else}
    <div
      style={`grid-column: ${$prefs.showAgentsSidebar ? 2 : 1}; grid-row: 1; display: flex; align-items: center; justify-content: center; color: var(--muted); min-height: 0;`}
    >
      Select an agent from the left sidebar
    </div>
  {/if}
</main>

<style>
  :root {
    color-scheme: light dark;
  }
  * {
    box-sizing: border-box;
  }
  :global(body),
  :global(html),
  :global(#app) {
    height: 100%;
    width: 100%;
    margin: 0;
  }

  .shell {
    display: grid;
    grid-template-columns: 1fr 280px;
    /* Allow the single row to shrink so inner scroll areas work */
    grid-template-rows: minmax(0, 1fr);
    height: 100vh;
    overflow: hidden;
    position: relative;
    font-family:
      system-ui,
      -apple-system,
      Segoe UI,
      Roboto,
      Ubuntu,
      Cantarell,
      "Noto Sans",
      "Helvetica Neue",
      Arial,
      "Apple Color Emoji",
      "Segoe UI Emoji";
  }

  /* Chat styles moved into ChatPane */

  /* Ensure the right sidebar grid item can shrink and allow inner scroll */
  /* Left stack container (agents on top, bottompane below) */
  .left-stack {
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    border-right: 1px solid var(--border);
  }
  .left-top {
    min-height: 120px;
    overflow: hidden;
  }
  .left-bottom {
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
  }
  .bottompane {
    height: 100%;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* Removed unused legacy right-sidebar resize styles */

  /* Horizontal splitter between left-top and left-bottom */
  .split-resize {
    height: 6px;
    cursor: ns-resize;
    background: transparent;
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }
  .split-resize:hover {
    background: var(--surface-2);
  }
  /* Full-height vertical resizer at the right edge of the left column */
  .left-col-resize {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: 6px;
    cursor: col-resize;
    background: transparent;
  }

  /* Right sidebar styles live in RightSidebar/child components */

  /* Floating show-left button when left sidebar is hidden */
  .shell > :global(.toggle-btn.floating) {
    position: absolute;
    left: 6px;
    top: 8px;
    z-index: 20;
  }
</style>
