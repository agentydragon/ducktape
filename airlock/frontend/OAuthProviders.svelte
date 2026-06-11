<script lang="ts">
  import { getApiClient } from "./api.ts";
  import type { OAuthProviderStatus } from "./types.ts";
  import { onMount } from "svelte";

  let providers = $state<OAuthProviderStatus[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  onMount(async () => {
    try {
      const api = await getApiClient();
      providers = await api.listOAuthProviders();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  function fmtExpiry(iso: string | null): string {
    if (!iso) return "N/A";
    return new Date(iso).toLocaleString();
  }

  function scopeDiff(requested: string[], granted: string): { missing: string[]; extra: string[]; drift: boolean } {
    const grantedSet = new Set(granted ? granted.split(/\s+/).filter(Boolean) : []);
    const requestedSet = new Set(requested);
    const missing = [...requestedSet].filter((s) => !grantedSet.has(s)).sort();
    const extra = [...grantedSet].filter((s) => !requestedSet.has(s)).sort();
    return { missing, extra, drift: missing.length + extra.length > 0 };
  }
</script>

<h2 class="section-heading text-xs font-semibold uppercase tracking-wider mb-3">OAuth Providers</h2>

{#if loading}
  <p class="section-heading italic py-4">Loading providers…</p>
{:else if error}
  <p class="font-medium" style="color: var(--color-error);">Failed to load providers: {error}</p>
{:else if providers.length === 0}
  <p class="section-heading italic py-4">No OAuth providers configured.</p>
{:else}
  <div class="space-y-4">
    {#each providers as provider (provider.name)}
      <div class="card rounded-lg shadow-sm p-5">
        <div class="flex items-start justify-between gap-4">
          <div class="flex-1">
            <h3 class="text-sm font-semibold m-0" style="color: var(--color-text);">{provider.display_name}</h3>
            <dl class="grid grid-cols-1 sm:grid-cols-[120px_1fr] gap-x-4 gap-y-1 text-sm mt-3">
              <dt class="section-heading font-semibold">Type</dt>
              <dd class="m-0"><code class="code-tag text-xs rounded px-1.5 py-0.5">{provider.provider_type}</code></dd>
              <dt class="section-heading font-semibold">Status</dt>
              <dd class="m-0">
                {#if provider.status.state === "connected"}
                  <span class="status-pill status-pill-done">Connected</span>
                {:else}
                  <span class="status-pill status-pill-pending">Not connected</span>
                {/if}
              </dd>
              <dt class="section-heading font-semibold">Requested</dt>
              <dd class="m-0" style="color: var(--color-text-muted);">
                {provider.requested_scopes.join(" ") || "(none)"}
              </dd>
              {#if provider.status.state === "connected"}
                <dt class="section-heading font-semibold">Expires</dt>
                <dd class="m-0" style="color: var(--color-text-muted);">{fmtExpiry(provider.status.expires_at)}</dd>
                <dt class="section-heading font-semibold">Granted</dt>
                <dd class="m-0" style="color: var(--color-text-muted);">{provider.status.scope || "(none)"}</dd>
                {@const diff = scopeDiff(provider.requested_scopes, provider.status.scope)}
                {#if diff.drift}
                  <dt class="section-heading font-semibold">Drift</dt>
                  <dd class="m-0" style="color: var(--color-warning, #b45309);">
                    {#if diff.missing.length > 0}missing: <code class="code-tag text-xs rounded px-1.5 py-0.5"
                        >{diff.missing.join(" ")}</code
                      >{/if}
                    {#if diff.missing.length > 0 && diff.extra.length > 0}<span>; </span>{/if}
                    {#if diff.extra.length > 0}extra: <code class="code-tag text-xs rounded px-1.5 py-0.5"
                        >{diff.extra.join(" ")}</code
                      >{/if}
                    — re-authorize to fix
                  </dd>
                {/if}
              {/if}
            </dl>
          </div>
          <a
            href="/oauth/authorize/{provider.name}"
            class="btn-approve font-semibold px-5 py-2.5 rounded-lg border-0 cursor-pointer transition-colors text-sm no-underline"
          >
            {provider.status.state === "connected" ? "Reconnect" : "Connect"}
          </a>
        </div>
      </div>
    {/each}
  </div>
{/if}
