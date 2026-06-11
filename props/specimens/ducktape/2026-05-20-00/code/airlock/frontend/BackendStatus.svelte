<script lang="ts">
  import { getApiClient } from "./api.ts";
  import type { BackendStatus } from "./types.ts";
  import { onMount } from "svelte";

  let backends = $state<BackendStatus[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  async function load(): Promise<void> {
    const api = await getApiClient();
    backends = await api.listBackends();
  }

  onMount(async () => {
    try {
      await load();
      const api = await getApiClient();
      api.onBackendsChanged(() => {
        load();
      });
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  function fmtSince(iso: string): string {
    return new Date(iso).toLocaleString();
  }
</script>

<h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-3">Backend Status</h2>

{#if loading}
  <p class="section-heading italic py-4">Loading backends…</p>
{:else if error}
  <p class="font-medium" style="color: var(--color-error);">Failed to load backends: {error}</p>
{:else if backends.length === 0}
  <p class="section-heading italic py-4">No backends configured.</p>
{:else}
  <div class="space-y-4">
    {#each backends as backend (backend.name)}
      <div class="card rounded-lg shadow-sm p-5">
        <div class="flex-1">
          <h3 class="text-sm font-semibold m-0" style="color: var(--color-text);">
            <code class="code-tag text-xs rounded px-1.5 py-0.5">{backend.name}</code>
          </h3>
          <dl class="grid grid-cols-1 sm:grid-cols-[120px_1fr] gap-x-4 gap-y-1 text-sm mt-3">
            <dt class="section-heading font-semibold">Status</dt>
            <dd class="m-0">
              {#if backend.connection_status.state === "connected"}
                <span class="status-pill status-pill-done">Connected</span>
              {:else}
                <span class="status-pill status-pill-pending">Degraded</span>
              {/if}
            </dd>
            {#if backend.connection_status.state === "degraded"}
              <dt class="section-heading font-semibold">Error</dt>
              <dd class="m-0" style="color: var(--color-error); word-break: break-word;">
                {backend.connection_status.error}
              </dd>
              <dt class="section-heading font-semibold">Since</dt>
              <dd class="m-0" style="color: var(--color-text-muted);">
                {fmtSince(backend.connection_status.since)}
              </dd>
            {/if}
          </dl>
        </div>
      </div>
    {/each}
  </div>
{/if}
