<script lang="ts">
  import { onMount } from "svelte";
  import { getMcpClient } from "./mcp.ts";
  import ActionList from "./ActionList.svelte";
  import ActionDetail from "./ActionDetail.svelte";
  import type { Action } from "./types.ts";

  const actionMatch = window.location.hash.match(/^#\/sessions\/([^/]+)\/actions\/(\d+)\/?$/);
  const sessionKey: string | null = actionMatch ? actionMatch[1] : null;
  const actionSeq: number | null = actionMatch ? parseInt(actionMatch[2], 10) : null;

  let pending = $state<Action[]>([]);
  let recent = $state<Action[]>([]);
  let action = $state<Action | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);

  async function loadList(): Promise<void> {
    const mcp = await getMcpClient();
    [pending, recent] = await Promise.all([mcp.listActions("pending"), mcp.listActions(undefined, 20)]);
  }

  onMount(async () => {
    if (sessionKey !== null && actionSeq !== null) {
      try {
        const mcp = await getMcpClient();
        const uri = `resource://sessions/${sessionKey}/actions/${actionSeq}`;
        await mcp.subscribeAction<Action>(uri, (a) => {
          action = a;
          loading = false;
          error = null;
        });
        if (loading) {
          error = "Failed to read action resource";
          loading = false;
        }
      } catch (err) {
        error = String(err);
        loading = false;
      }
    } else {
      try {
        await loadList();
      } catch (e) {
        error = String(e);
      } finally {
        loading = false;
      }
      const mcp = await getMcpClient();
      mcp.onListChanged(() => {
        loadList();
      });
    }
  });
</script>

{#snippet defaultHeader()}
  <header class="app-header px-4 py-3 sm:px-6 flex items-center gap-3">
    <h1 class="app-header-title text-lg font-semibold m-0">Airlock</h1>
  </header>
{/snippet}

{#if loading}
  {@render defaultHeader()}
  <main class="max-w-4xl mx-auto px-4 py-6"><p class="section-heading">Loading…</p></main>
{:else if error}
  {@render defaultHeader()}
  <main class="max-w-4xl mx-auto px-4 py-6">
    <p class="font-medium" style="color: var(--color-error);">Failed to load: {error}</p>
  </main>
{:else if sessionKey !== null && actionSeq !== null}
  {#if action}
    <ActionDetail {action} />
  {:else}
    {@render defaultHeader()}
    <main class="max-w-4xl mx-auto px-4 py-6">
      <p class="font-medium" style="color: var(--color-error);">Action not found.</p>
    </main>
  {/if}
{:else}
  <ActionList {pending} {recent} />
{/if}
