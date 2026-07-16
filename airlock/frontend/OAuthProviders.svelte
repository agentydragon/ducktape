<script lang="ts">
  import { getApiClient } from "./api.ts";
  import type { OAuthProviderStatus } from "./types.ts";
  import { onMount } from "svelte";

  let providers = $state<OAuthProviderStatus[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  type ScopeRow = {
    scope: string;
    requested: boolean;
    granted: boolean;
  };
  type ScopeComparison = {
    rows: ScopeRow[];
    missing: string[];
    extra: string[];
    drift: boolean;
  };

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

  function compareScopes(requested: string[], granted: string): ScopeComparison {
    const requestedScopes = [...new Set(requested)];
    const requestedSet = new Set(requestedScopes);
    const grantedSet = new Set(granted ? granted.split(/\s+/).filter(Boolean) : []);
    const missing = requestedScopes.filter((scope) => !grantedSet.has(scope)).sort();
    const extra = [...grantedSet].filter((scope) => !requestedSet.has(scope)).sort();
    return {
      rows: [...requestedScopes, ...extra].map((scope) => ({
        scope,
        requested: requestedSet.has(scope),
        granted: grantedSet.has(scope),
      })),
      missing,
      extra,
      drift: missing.length + extra.length > 0,
    };
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
      {@const grantedScope =
        provider.status.state === "connected" || provider.status.state === "expired" ? provider.status.scope : ""}
      {@const scopes = compareScopes(provider.requested_scopes, grantedScope)}
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
                  <span class="status-pill status-pill-connected">Connected</span>
                {:else if provider.status.state === "expired"}
                  <span class="status-pill status-pill-expired">Token expired — refresh failing</span>
                {:else}
                  <span class="status-pill status-pill-disconnected">Not connected</span>
                {/if}
              </dd>
              <dt class="section-heading font-semibold">Scopes</dt>
              <dd class="m-0 min-w-0">
                <div class="scope-table-wrap overflow-x-auto rounded-md">
                  <table class="scope-table min-w-full border-collapse text-xs">
                    <thead>
                      <tr class="thead-row">
                        <th class="th-cell px-3 py-2 text-left font-semibold">Scope</th>
                        <th class="th-cell px-3 py-2 text-center font-semibold">Requested</th>
                        <th class="th-cell px-3 py-2 text-center font-semibold">Granted</th>
                      </tr>
                    </thead>
                    <tbody class="tbody">
                      {#if scopes.rows.length === 0}
                        <tr>
                          <td class="px-3 py-2 italic" colspan="3" style="color: var(--color-text-muted);">No scopes</td
                          >
                        </tr>
                      {:else}
                        {#each scopes.rows as row (row.scope)}
                          <tr class="data-row">
                            <td class="scope-cell px-3 py-2 font-mono">{row.scope}</td>
                            <td class="px-3 py-2 text-center">
                              <span class={row.requested ? "scope-mark scope-mark-yes" : "scope-mark scope-mark-no"}>
                                {row.requested ? "✓" : "✕"}
                              </span>
                            </td>
                            <td class="px-3 py-2 text-center">
                              <span class={row.granted ? "scope-mark scope-mark-yes" : "scope-mark scope-mark-no"}>
                                {row.granted ? "✓" : "✕"}
                              </span>
                            </td>
                          </tr>
                        {/each}
                      {/if}
                    </tbody>
                  </table>
                </div>
              </dd>
              {#if provider.status.state === "connected" || provider.status.state === "expired"}
                <dt class="section-heading font-semibold">Access token</dt>
                <dd class="m-0" style="color: var(--color-text-muted);">
                  {provider.status.state === "expired"
                    ? `Expired ${fmtExpiry(provider.status.expires_at)}`
                    : `Expires ${fmtExpiry(provider.status.expires_at)}`}
                </dd>
                {#if scopes.drift}
                  <dt class="section-heading font-semibold">Drift</dt>
                  <dd class="m-0" style="color: var(--color-warning, #b45309);">
                    {#if scopes.missing.length > 0}missing: <code class="code-tag text-xs rounded px-1.5 py-0.5"
                        >{scopes.missing.join(" ")}</code
                      >{/if}
                    {#if scopes.missing.length > 0 && scopes.extra.length > 0}<span>; </span>{/if}
                    {#if scopes.extra.length > 0}extra: <code class="code-tag text-xs rounded px-1.5 py-0.5"
                        >{scopes.extra.join(" ")}</code
                      >{/if}
                    — re-authorize to fix
                  </dd>
                {/if}
                {#if provider.status.state === "expired" && provider.status.last_refresh_error}
                  <dt class="section-heading font-semibold">Refresh error</dt>
                  <dd class="m-0">
                    <code
                      class="code-tag text-xs rounded px-1.5 py-0.5 block whitespace-pre-wrap"
                      style="color: var(--color-error);">{provider.status.last_refresh_error}</code
                    >
                  </dd>
                {/if}
              {/if}
            </dl>
          </div>
          <a
            href="/oauth/authorize/{provider.name}"
            class="btn-primary font-semibold px-5 py-2.5 rounded-lg border-0 cursor-pointer transition-colors text-sm no-underline"
          >
            {provider.status.state === "connected" ? "Reconnect" : "Connect"}
          </a>
        </div>
      </div>
    {/each}
  </div>
{/if}

<style>
  .scope-table-wrap {
    border: 1px solid var(--color-border);
  }

  .scope-cell {
    color: var(--color-text-code);
    overflow-wrap: anywhere;
  }

  .scope-mark {
    font-weight: 700;
  }

  .scope-mark-yes {
    color: var(--color-success);
  }

  .scope-mark-no {
    color: var(--color-text-muted);
  }
</style>
